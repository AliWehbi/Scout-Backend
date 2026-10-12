"""
Effective-permission computation.

effective = (role permissions x role scope) U (direct grants), ignoring expired grants.
A scope is either the whole organization (represented as `None`) or a unit id.
"""

from collections import defaultdict
from functools import cached_property

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .catalog import ALL_PERMISSIONS, DEFAULT_ROLES, PERMISSION_GROUP, SHARED_STORE_PERMISSIONS
from .models import Permission, PermissionGrant, Role, RoleAssignment

ORG_WIDE = None


class PermissionService:
    def __init__(self, membership):
        self.membership = membership

    @cached_property
    def scopes(self) -> dict[str, set[int | None]]:
        """codename -> set of scopes (unit ids, or None for organization-wide)."""
        result: dict[str, set[int | None]] = defaultdict(set)
        membership = self.membership
        if membership is None or not membership.is_active:
            return result

        if membership.is_owner:
            for codename in ALL_PERMISSIONS:
                result[codename].add(ORG_WIDE)
            return result

        assignments = RoleAssignment.objects.filter(membership=membership).prefetch_related("role__permissions")
        for assignment in assignments:
            for perm in assignment.role.permissions.all():
                if assignment.unit_id is None or perm.codename in SHARED_STORE_PERMISSIONS:
                    result[perm.codename].add(ORG_WIDE)
                elif perm.unit_scoped:
                    result[perm.codename].add(assignment.unit_id)

        grants = (
            PermissionGrant.objects.filter(membership=membership)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
            .select_related("permission")
        )
        for grant in grants:
            codename = grant.permission.codename
            result[codename].add(ORG_WIDE if codename in SHARED_STORE_PERMISSIONS else grant.unit_id)
        return result

    def has_perm(self, codename: str, unit=None) -> bool:
        """True if granted organization-wide, or for `unit` when one is given."""
        scopes = self.scopes.get(codename)
        if not scopes:
            return False
        if ORG_WIDE in scopes:
            return True
        unit_id = getattr(unit, "pk", unit)
        return unit_id is not None and unit_id in scopes

    def has_perm_anywhere(self, codename: str) -> bool:
        return bool(self.scopes.get(codename))

    def unit_ids(self, codename: str) -> set[int] | None:
        """Units the permission applies to; `None` means every unit (organization-wide)."""
        scopes = self.scopes.get(codename, set())
        if ORG_WIDE in scopes:
            return None
        return {s for s in scopes if s is not None}

    def as_list(self) -> list[dict]:
        out = []
        for codename in ALL_PERMISSIONS:
            scopes = self.scopes.get(codename)
            if not scopes:
                continue
            out.append(
                {
                    "codename": codename,
                    "org_wide": ORG_WIDE in scopes,
                    "unit_ids": sorted(s for s in scopes if s is not None),
                }
            )
        return out


def sync_permission_catalog() -> None:
    """Make the Permission table match catalog.py."""
    for codename, definition in ALL_PERMISSIONS.items():
        Permission.objects.update_or_create(
            codename=codename,
            defaults={"group": PERMISSION_GROUP[codename], "unit_scoped": definition.unit_scoped},
        )
    Permission.objects.exclude(codename__in=ALL_PERMISSIONS).delete()


@transaction.atomic
def create_default_roles(organization) -> dict[str, Role]:
    sync_permission_catalog()
    perms = {p.codename: p for p in Permission.objects.all()}
    roles = {}
    for spec in DEFAULT_ROLES:
        role, _ = Role.objects.get_or_create(
            organization=organization,
            key=spec["key"],
            defaults={"name": str(spec["name"]), "description": str(spec["description"]), "is_system": True},
        )
        role.permissions.set([perms[c] for c in spec["permissions"]])
        roles[spec["key"]] = role
    return roles
