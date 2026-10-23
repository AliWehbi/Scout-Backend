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
from apps.customfields import services as custom_field_services
from apps.permissions.drf import HasOrgPermission

from . import registry, services
from .models import DocumentKind, DocumentTemplate, file_format
from .serializers import (
    DocumentTemplateSerializer,
    ExportParamsSerializer,
    StarterParamsSerializer,
    ValuesParamsSerializer,
)


class DocumentThrottle(UserRateThrottle):
    """Every render starts a process, so cap how often one login can ask for documents."""

    scope = "documents"
    rate = "30/min"


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def docx_response(data: bytes, filename: str, fmt: str = "docx") -> FileResponse:
    mime = XLSX_MIME if fmt == "xlsx" else services.DOCX_MIME
    response = FileResponse(io.BytesIO(data), as_attachment=True, filename=filename, content_type=mime)
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
        fmt = file_format(template.kind)
        name = template.original_filename or f"{template.name}.{fmt}"
        if not name.lower().endswith(f".{fmt}"):
            name += f".{fmt}"
        return docx_response(services.stored_bytes(template), services.clean_filename(name), fmt)

    @action(detail=False, methods=["get"])
    def fields(self, request):
        return Response(
            [
                {"kind": kind, "label": label, "fields": registry.fields_for(kind, self.organization)}
                for kind, label in DocumentKind.choices
            ]
        )

    @action(detail=False, methods=["get"])
    def starter(self, request):
        params = StarterParamsSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        kind, lang = params.validated_data["kind"], params.validated_data["lang"]
        fmt = file_format(kind)
        return docx_response(services.starter_bytes(kind, lang), f"{kind}.{lang}.{fmt}", fmt)


def pick_template(data, organization, kind) -> DocumentTemplate | None:
    if data.get("template"):
        return get_object_or_404(DocumentTemplate, organization=organization, kind=kind, pk=data["template"])
    if "template" in data:  # explicitly null: the built-in template
        return None
    return services.default_template(organization, kind)


def clean_overrides(raw: dict, organization, entities=registry.DOCUMENT_ENTITIES) -> dict:
    """Per-document values, by template code (event.cedar_bus, org.cedar_phone), checked like stored values."""
    fields = {f.template_code(organization.code): f for f in registry.coded_fields(organization, entities)}
    cleaned, errors = {}, {}
    for code, value in raw.items():
        field = fields.get(code)
        if field is None:
            errors[code] = [_("Unknown field.")]
            continue
        try:
            cleaned[code] = custom_field_services.clean_value(field, value)
        except ValidationError as exc:
            errors[code] = exc.detail if isinstance(exc.detail, list) else [exc.detail]
    if errors:
        raise ValidationError({"values": errors})
    return cleaned


def values_response(request, *, event, service, organization):
    """
    `GET /events/{id}/document-values/`: the organization's custom fields used by the chosen
    template, with the event's value (or the default) to check before exporting.
    """
    params = ValuesParamsSerializer(data=request.query_params)
    params.is_valid(raise_exception=True)
    data = params.validated_data
    kind = data["kind"]
    if kind not in services.allowed_kinds(service, event):
        raise PermissionDenied(_("You can't export this document for this event."))
    template = pick_template(data, organization, kind)
    if template is None:
        return Response([])  # the built-in templates only use the app's own fields
    used = services.used_custom_codes(services.stored_bytes(template), organization.code)
    rows = []
    for field in registry.coded_fields(organization):
        template_code = field.template_code(organization.code)
        if template_code not in used:
            continue
        value, source = registry.field_value(field, event)
        rows.append(
            {
                "code": field.code,
                "entity": field.entity,
                "template_code": template_code,
                "label_en": field.label_en,
                "label_ar": field.label_ar,
                "field_type": field.field_type,
                "options": field.options,
                "option_labels": field.option_labels,
                "value": value,
                "source": source,
                "event_value": (event.custom_data or {}).get(field.key) if field.entity == "event" else None,
                "default": field.default,
            }
        )
    return Response(rows)


def export_response(request, *, event, service, organization):
    """`GET /events/{id}/export/`: the event filled into a Word template."""
    params = ExportParamsSerializer(data=request.query_params)
    params.is_valid(raise_exception=True)
    data = params.validated_data
    kind = data["kind"]
    if kind not in services.allowed_kinds(service, event):
        raise PermissionDenied(_("You can't export this document for this event."))
    overrides = clean_overrides(data.get("values") or {}, organization)
    lang = data.get("lang") or (request.LANGUAGE_CODE if request.LANGUAGE_CODE in registry.LANGS else "en")
    day = data.get("date")
    if kind == DocumentKind.ATTENDANCE_SHEET and day is not None and day not in event.days:
        raise ValidationError({"date": [_("This day isn't part of the event.")]})

    template = pick_template(data, organization, kind)
    document = services.export_event(
        event, kind=kind, template=template, lang=lang, user=request.user, day=day, overrides=overrides
    )
    log_action(
        request,
        "event.exported",
        organization=organization,
        target=event,
        changes={
            "kind": kind,
            "template": template.pk if template else None,
            "lang": lang,
            "edited_values": sorted(overrides),
        },
    )
    return docx_response(document, services.export_filename(event, kind, lang, day))
