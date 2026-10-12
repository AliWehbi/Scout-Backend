import secrets

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.members import services as member_services
from apps.permissions.models import PermissionGrant, RoleAssignment
from apps.permissions.service import create_default_roles
from apps.units.models import Unit

from .models import (
    Invitation,
    MembershipStatus,
    Organization,
    OrganizationSettings,
    OrgMembership,
)

User = get_user_model()


def generate_username(first_name: str, last_name: str = "") -> str:
    parts = [slugify(part, allow_unicode=False).replace("-", "") for part in (first_name, last_name)]
    base = ".".join(p for p in parts if p)[:24] or "member"
    candidate = base
    while User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{base}.{secrets.randbelow(9000) + 1000}"
    return candidate


@transaction.atomic
def signup_organization(*, organization: dict, admin: dict, units: list[dict], branding: dict) -> OrgMembership:
    org = Organization.objects.create(**organization)
    OrganizationSettings.objects.create(organization=org, **branding)
    roles = create_default_roles(org)

    password = admin.pop("password")
    user = User.objects.create_user(**admin, password=password)
    if admin.get("preferred_language") is None:
        user.preferred_language = org.default_language
        user.save(update_fields=["preferred_language"])

    membership = OrgMembership.objects.create(organization=org, user=user, is_owner=True)
    RoleAssignment.objects.create(membership=membership, role=roles["org_admin"], assigned_by=user)
    member_services.ensure_member(membership, kind="adult")
    for unit in units:
        Unit.objects.create(organization=org, **unit)
    return membership


def replace_role_assignments(membership, assignments: list[dict], actor) -> None:
    RoleAssignment.objects.filter(membership=membership).delete()
    RoleAssignment.objects.bulk_create(
        [
            RoleAssignment(membership=membership, role=a["role"], unit=a.get("unit"), assigned_by=actor)
            for a in assignments
        ]
    )


def replace_unit_memberships(membership, unit_memberships: list[dict]) -> None:
    """Units belong to the person (Member); a login without one gets a Member first."""
    member = member_services.ensure_member(membership)
    member_services.replace_unit_memberships(member, unit_memberships)


def replace_permission_grants(membership, grants: list[dict], actor) -> dict:
    """Replace the extra grants of a membership. Returns {added: [...], removed: [...]} for auditing."""

    def key(permission_codename, unit_id):
        return f"{permission_codename}@{unit_id or 'org'}"

    existing = {
        key(g.permission.codename, g.unit_id): g for g in membership.permission_grants.select_related("permission")
    }
    wanted = {key(g["permission"].codename, getattr(g.get("unit"), "pk", None)): g for g in grants}

    removed = [k for k in existing if k not in wanted]
    added = [k for k in wanted if k not in existing]
    PermissionGrant.objects.filter(pk__in=[existing[k].pk for k in removed]).delete()

    for k, data in wanted.items():
        if k in existing:
            grant = existing[k]
            grant.expires_at = data.get("expires_at")
            grant.note = data.get("note", "")
            grant.save(update_fields=["expires_at", "note", "updated_at"])
        else:
            PermissionGrant.objects.create(
                membership=membership,
                permission=data["permission"],
                unit=data.get("unit"),
                expires_at=data.get("expires_at"),
                note=data.get("note", ""),
                granted_by=actor,
            )
    return {"added": added, "removed": removed}


def create_invitation(membership, actor) -> Invitation:
    now = timezone.now()
    membership.invitations.filter(accepted_at__isnull=True, revoked_at__isnull=True).update(revoked_at=now)
    return Invitation.objects.create(membership=membership, created_by=actor)


@transaction.atomic
def add_member(*, organization, actor, data: dict) -> tuple[OrgMembership, Invitation | None]:
    invite = data.get("invite", False)
    username = data.get("username") or generate_username(data["first_name"], data.get("last_name", ""))
    user = User(
        username=username,
        email=data.get("email") or None,
        first_name=data["first_name"],
        last_name=data.get("last_name", ""),
        preferred_language=data.get("preferred_language") or organization.default_language,
    )
    if data.get("password"):
        user.set_password(data["password"])
    else:
        user.set_unusable_password()
    user.save()

    membership = OrgMembership.objects.create(
        organization=organization,
        user=user,
        status=MembershipStatus.INVITED if invite else MembershipStatus.ACTIVE,
    )
    replace_role_assignments(membership, data.get("roles", []), actor)
    if data.get("units"):
        replace_unit_memberships(membership, data["units"])
    else:
        member_services.sync_person_records(membership)
    invitation = create_invitation(membership, actor) if invite else None
    return membership, invitation


@transaction.atomic
def accept_invitation(invitation: Invitation, *, data: dict, current_user=None) -> OrgMembership:
    """
    Activate an invited membership.

    - Anonymous: completes the placeholder account (username, password, optional email).
    - Signed in as a different user: the membership moves to that existing account, and the
      unused placeholder account is removed.
    """
    membership = invitation.membership
    placeholder = membership.user

    if current_user is not None and current_user.is_authenticated and current_user.pk != placeholder.pk:
        organization = membership.organization
        Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        existing = OrgMembership.objects.filter(organization=organization, user=current_user).first()
        if existing is not None:
            # Already a member: the invitation is consumed and the duplicate membership dropped.
            membership.delete()
            membership = existing
        else:
            membership.user = current_user
            membership.status = MembershipStatus.ACTIVE
            membership.save(update_fields=["user", "status", "updated_at"])
        if not placeholder.has_usable_password() and not placeholder.memberships.exists():
            placeholder.delete()
        return membership
    else:
        user = placeholder
        user.username = data["username"]
        if data.get("email"):
            user.email = data["email"]
        for field in ("first_name", "last_name", "preferred_language"):
            if data.get(field):
                setattr(user, field, data[field])
        user.set_password(data["password"])
        user.save()
        membership.status = MembershipStatus.ACTIVE
        membership.save(update_fields=["status", "updated_at"])

    Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
    return membership
