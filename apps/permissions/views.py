from django.db.models import Count
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin

from .catalog import CATALOG
from .drf import HasOrgPermission
from .models import Role
from .serializers import RoleSerializer


class PermissionCatalogView(APIView):
    """The permission catalog grouped by feature, used to render the permission matrix."""

    def get(self, request):
        return Response(
            [
                {
                    "key": group.key,
                    "label": str(group.label),
                    "permissions": [
                        {"codename": p.codename, "label": str(p.label), "unit_scoped": p.unit_scoped}
                        for p in group.permissions
                    ],
                }
                for group in CATALOG
            ]
        )


class RoleViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = RoleSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {
        "list": "users.view",
        "retrieve": "users.view",
        "create": "permissions.manage",
        "update": "permissions.manage",
        "partial_update": "permissions.manage",
        "destroy": "permissions.manage",
    }
    search_fields = ["name"]

    def get_queryset(self):
        return (
            Role.objects.filter(organization=self.organization)
            .prefetch_related("permissions")
            .annotate(assignment_count=Count("assignments__membership", distinct=True))
            .order_by("-is_system", "name")
        )

    def perform_create(self, serializer):
        role = serializer.save(organization=self.organization)
        log_action(self.request, "role.created", organization=self.organization, target=role)

    def perform_update(self, serializer):
        before = sorted(p.codename for p in serializer.instance.permissions.all())
        role = serializer.save()
        after = sorted(p.codename for p in role.permissions.all())
        log_action(
            self.request,
            "role.updated",
            organization=self.organization,
            target=role,
            changes={"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))},
        )

    def perform_destroy(self, instance):
        if instance.is_system:
            raise ValidationError({"detail": _("Default roles can't be deleted. You can edit or duplicate them.")})
        log_action(self.request, "role.deleted", organization=self.organization, target=instance)
        instance.delete()
