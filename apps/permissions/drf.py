from rest_framework.permissions import BasePermission

from apps.core.tenancy import get_active_membership

from .service import PermissionService


def get_permission_service(request) -> PermissionService:
    if not hasattr(request, "_permission_service"):
        request._permission_service = PermissionService(get_active_membership(request))
    return request._permission_service


class IsOrgMember(BasePermission):
    """Any active member of the organization selected for this request."""

    def has_permission(self, request, view):
        get_active_membership(request)  # raises 403 when not a member
        return True


class HasOrgPermission(BasePermission):
    """
    Checks the catalog permission required for the current action.

    Views declare `permission_map = {"list": "users.view", "create": "users.manage", ...}`.
    Actions missing from the map only require membership. A value of `None` also means
    membership only. Unit-scoped objects can implement `get_permission_unit(obj)` on the
    view so object-level checks honour unit-scoped grants.
    """

    def _codename(self, view):
        permission_map = getattr(view, "permission_map", {})
        action = getattr(view, "action", None) or view.request.method.lower()
        return permission_map.get(action, permission_map.get("*"))

    def has_permission(self, request, view):
        service = get_permission_service(request)
        codename = self._codename(view)
        if codename is None:
            return True
        return service.has_perm_anywhere(codename)

    def has_object_permission(self, request, view, obj):
        codename = self._codename(view)
        if codename is None:
            return True
        unit = view.get_permission_unit(obj) if hasattr(view, "get_permission_unit") else None
        return get_permission_service(request).has_perm(codename, unit)
