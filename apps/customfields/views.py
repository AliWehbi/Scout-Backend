from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin
from apps.permissions.drf import HasOrgPermission

from .models import CustomField
from .registry import ENTITIES
from .serializers import CustomFieldSerializer


class CustomFieldViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """`?entity=member|event` picks the kind of record (members when left out)."""

    serializer_class = CustomFieldSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {
        "create": "settings.manage",
        "update": "settings.manage",
        "partial_update": "settings.manage",
        "destroy": "settings.manage",
        "reorder": "settings.manage",
    }
    pagination_class = None

    def entity(self) -> str:
        entity = self.request.query_params.get("entity") or "member"
        if entity not in ENTITIES:
            raise ValidationError({"entity": ["Unknown entity."]})
        return entity

    def get_queryset(self):
        qs = CustomField.objects.filter(organization=self.organization).prefetch_related("event_types")
        if self.action in ("list", "reorder"):
            qs = qs.filter(entity=self.entity())
        return qs.order_by("order", "id")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "organization": self.organization}

    def perform_create(self, serializer):
        entity = serializer.validated_data.get("entity", "member")
        last = CustomField.objects.for_entity(self.organization, entity).order_by("-order").first()
        field = serializer.save(organization=self.organization, order=(last.order + 1) if last else 0)
        log_action(self.request, "custom_field.created", organization=self.organization, target=field)

    def perform_update(self, serializer):
        field = serializer.save()
        log_action(self.request, "custom_field.updated", organization=self.organization, target=field)

    def perform_destroy(self, instance):
        log_action(self.request, "custom_field.deleted", organization=self.organization, target=instance)
        instance.delete()

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        ids = request.data.get("ids", [])
        fields = {f.pk: f for f in self.get_queryset()}
        for index, field_id in enumerate(ids):
            if field_id in fields:
                fields[field_id].order = index
                fields[field_id].save(update_fields=["order"])
        return Response(self.get_serializer(self.get_queryset(), many=True).data)
