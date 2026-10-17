"""
The fixed catalog of permissions. This is the single source of truth: the `Permission`
table is synced from it after every migrate, and the frontend renders the permission
matrix from `GET /api/v1/permissions/catalog/`.

`unit_scoped=True` means the permission can be granted for specific units; otherwise it
only makes sense organization-wide.
"""

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class PermissionDef:
    codename: str
    label: str
    unit_scoped: bool


@dataclass(frozen=True)
class PermissionGroup:
    key: str
    label: str
    permissions: tuple[PermissionDef, ...]


CATALOG: tuple[PermissionGroup, ...] = (
    PermissionGroup(
        "members",
        _("Members"),
        (
            PermissionDef("members.view", _("View members"), True),
            PermissionDef("members.edit", _("Edit members"), True),
            PermissionDef("members.view_medical", _("View medical info"), True),
        ),
    ),
    PermissionGroup(
        "events",
        _("Events"),
        (
            PermissionDef("events.view", _("View events"), True),
            PermissionDef("events.manage", _("Manage events"), True),
            PermissionDef("events.check_in", _("Check in attendance"), True),
        ),
    ),
    PermissionGroup(
        "finance",
        _("Finance"),
        (
            PermissionDef("finance.view", _("View finances"), False),
            PermissionDef("finance.manage", _("Manage finances"), False),
        ),
    ),
    PermissionGroup(
        "inventory",
        _("Inventory"),
        (
            PermissionDef("inventory.view", _("View inventory"), False),
            PermissionDef("inventory.check_out", _("Check out & return equipment"), False),
            PermissionDef("inventory.manage", _("Manage inventory"), False),
        ),
    ),
    PermissionGroup(
        "administration",
        _("Settings & access"),
        (
            PermissionDef("users.view", _("View users"), False),
            PermissionDef("users.manage", _("Manage users"), False),
            PermissionDef("units.manage", _("Manage units"), False),
            PermissionDef("permissions.manage", _("Manage roles & permissions"), False),
            PermissionDef("settings.manage", _("Manage organization settings"), False),
            PermissionDef("audit.view", _("View audit log"), False),
        ),
    ),
)

ALL_PERMISSIONS: dict[str, PermissionDef] = {p.codename: p for g in CATALOG for p in g.permissions}

# The equipment store and the books are shared by the whole organization, so these apply
# organization-wide even when they come from a role assigned for one unit (e.g. a Cubs unit leader).
ORG_WIDE_PERMISSIONS = frozenset(
    {"inventory.view", "inventory.check_out", "inventory.manage", "finance.view", "finance.manage"}
)
PERMISSION_GROUP: dict[str, str] = {p.codename: g.key for g in CATALOG for p in g.permissions}


# Roles created for every new organization. Admins can edit or duplicate them later.
DEFAULT_ROLES: tuple[dict, ...] = (
    {
        "key": "org_admin",
        "name": _("Org Admin"),
        "description": _("Full access to everything in the organization."),
        "permissions": tuple(ALL_PERMISSIONS),
    },
    {
        "key": "unit_leader",
        "name": _("Unit Leader"),
        "description": _("Runs a unit: members, events and attendance."),
        "permissions": (
            "members.view",
            "members.edit",
            "members.view_medical",
            "events.view",
            "events.manage",
            "events.check_in",
            "inventory.view",
            "inventory.check_out",
        ),
    },
    {
        "key": "assistant_leader",
        "name": _("Assistant Leader"),
        "description": _("Helps run a unit."),
        "permissions": ("members.view", "events.view", "events.check_in"),
    },
    {
        "key": "treasurer",
        "name": _("Treasurer"),
        "description": _("Keeps the books: income, expenses and currencies."),
        "permissions": ("finance.view", "finance.manage"),
    },
    {
        "key": "quartermaster",
        "name": _("Quartermaster"),
        "description": _("Looks after equipment and stores."),
        "permissions": ("inventory.view", "inventory.check_out", "inventory.manage"),
    },
    {
        "key": "parent",
        "name": _("Parent"),
        "description": _("Parent or guardian of one or more scouts."),
        "permissions": (),
    },
    {
        "key": "scout",
        "name": _("Scout"),
        "description": _("Youth member."),
        "permissions": (),
    },
)
