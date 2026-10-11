from rest_framework import mixins, permissions, viewsets

from apps.core.tenancy import TenantScopedMixin
from apps.permissions.drf import HasOrgPermission

from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogViewSet(TenantScopedMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = AuditLog.objects.select_related("actor")
    serializer_class = AuditLogSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {"*": "audit.view"}
    filterset_fields = {
        "action": ["exact", "startswith"],
        "actor": ["exact"],
        "target_type": ["exact"],
        "target_id": ["exact"],
        "created_at": ["gte", "lte"],
    }
    search_fields = ["target_repr", "action", "actor__username"]
