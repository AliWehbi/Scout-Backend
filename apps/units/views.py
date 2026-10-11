from django.db.models import Count, Prefetch
from rest_framework import permissions, viewsets

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin
from apps.permissions.drf import HasOrgPermission

from .models import Unit, UnitMembership
from .serializers import UnitSerializer


class UnitViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """Units are visible to every member; changing them requires `units.manage`."""

    serializer_class = UnitSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {
        "create": "units.manage",
        "update": "units.manage",
        "partial_update": "units.manage",
        "destroy": "units.manage",
    }
    search_fields = ["name_en", "name_ar", "section"]
    filterset_fields = ["is_active"]

    def get_queryset(self):
        return (
            Unit.objects.filter(organization=self.organization)
            .annotate(member_count=Count("memberships", distinct=True))
            .prefetch_related(
                Prefetch("memberships", queryset=UnitMembership.objects.select_related("membership__user"))
            )
            .order_by("age_min", "name_en")
        )

    def perform_create(self, serializer):
        unit = serializer.save(organization=self.organization)
        log_action(self.request, "unit.created", organization=self.organization, target=unit)

    def perform_update(self, serializer):
        unit = serializer.save()
        log_action(
            self.request,
            "unit.updated",
            organization=self.organization,
            target=unit,
            changes={"fields": sorted(serializer.validated_data.keys())},
        )

    def perform_destroy(self, instance):
        log_action(self.request, "unit.deleted", organization=self.organization, target=instance)
        instance.delete()
