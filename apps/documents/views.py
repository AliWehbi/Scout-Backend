import io

from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin
from apps.permissions.drf import HasOrgPermission

from . import registry, services
from .models import DocumentKind, DocumentTemplate
from .serializers import DocumentTemplateSerializer, ExportParamsSerializer, StarterParamsSerializer


class DocumentThrottle(UserRateThrottle):
    """Every render starts a process, so cap how often one login can ask for documents."""

    scope = "documents"
    rate = "30/min"


def docx_response(data: bytes, filename: str) -> FileResponse:
    response = FileResponse(io.BytesIO(data), as_attachment=True, filename=filename, content_type=services.DOCX_MIME)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


class DocumentTemplateViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """
    Word templates of the organization. Anyone who can see events may list them (to pick one
    when exporting); uploading and managing them needs `settings.manage`.
    """

    serializer_class = DocumentTemplateSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {"list": "events.view", "*": "settings.manage"}
    http_method_names = ["get", "post", "patch", "delete"]
    pagination_class = None

    def get_queryset(self):
        qs = DocumentTemplate.objects.filter(organization=self.organization).select_related("uploaded_by")
        kind = self.request.query_params.get("kind")
        return qs.filter(kind=kind) if kind else qs

    def get_throttles(self):
        if self.action in ("create", "starter", "download"):
            return [DocumentThrottle()]
        return super().get_throttles()

    def perform_create(self, serializer):
        template = serializer.save()
        log_action(
            self.request,
            "document_template.uploaded",
            organization=self.organization,
            target=template,
            changes={"kind": template.kind, "sha256": template.sha256},
        )

    def perform_update(self, serializer):
        template = serializer.save()
        log_action(self.request, "document_template.updated", organization=self.organization, target=template)

    def perform_destroy(self, instance):
        log_action(self.request, "document_template.deleted", organization=self.organization, target=instance)
        file = instance.file
        instance.delete()
        transaction.on_commit(lambda: file.delete(save=False))

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        template = self.get_object()
        name = template.original_filename or f"{template.name}.docx"
        if not name.lower().endswith(".docx"):
            name += ".docx"
        return docx_response(services.stored_bytes(template), services.clean_filename(name))

    @action(detail=False, methods=["get"])
    def fields(self, request):
        return Response(
            [
                {"kind": kind, "label": label, "fields": registry.fields_for(kind)}
                for kind, label in DocumentKind.choices
            ]
        )

    @action(detail=False, methods=["get"])
    def starter(self, request):
        params = StarterParamsSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        kind, lang = params.validated_data["kind"], params.validated_data["lang"]
        return docx_response(services.starter_bytes(kind, lang), f"{kind}.{lang}.docx")


def export_response(request, *, event, service, organization):
    """`GET /events/{id}/export/`: the event filled into a Word template."""
    params = ExportParamsSerializer(data=request.query_params)
    params.is_valid(raise_exception=True)
    data = params.validated_data
    kind = data["kind"]
    if kind not in services.allowed_kinds(service, event):
        raise PermissionDenied(_("You can't export this document for this event."))
    lang = data.get("lang") or (request.LANGUAGE_CODE if request.LANGUAGE_CODE in registry.LANGS else "en")
    day = data.get("date")
    if kind == DocumentKind.ATTENDANCE_SHEET and day is not None and day not in event.days:
        raise ValidationError({"date": [_("This day isn't part of the event.")]})

    if data.get("template"):
        template = get_object_or_404(DocumentTemplate, organization=organization, kind=kind, pk=data["template"])
    elif "template" in data:  # explicitly null: the built-in template
        template = None
    else:
        template = services.default_template(organization, kind)

    document = services.export_event(event, kind=kind, template=template, lang=lang, user=request.user, day=day)
    log_action(
        request,
        "event.exported",
        organization=organization,
        target=event,
        changes={"kind": kind, "template": template.pk if template else None, "lang": lang},
    )
    return docx_response(document, services.export_filename(event, kind, lang, day))
