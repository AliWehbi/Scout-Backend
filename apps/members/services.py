"""
Business rules for members: linking logins to people, unit-scoped visibility, custom-field
validation, and applying parent change requests.
"""

from datetime import date

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from apps.units.models import UnitMembership, UnitPosition

from .models import (
    AppliesTo,
    ChangeRequest,
    CustomField,
    FieldType,
    Guardian,
    MedicalInfo,
    Member,
    MemberKind,
    MemberStatus,
    RequestStatus,
)

ADULT_ROLE_KEYS = {"org_admin", "unit_leader", "assistant_leader", "treasurer", "quartermaster"}
LEADER_POSITIONS = (UnitPosition.LEADER, UnitPosition.ASSISTANT)

# Fields a parent may ask to change (custom fields are added per organization).
PARENT_EDITABLE_FIELDS = {
    "phone",
    "email",
    "address",
    "medical.allergies",
    "medical.medications",
    "medical.emergency_contacts",
    "medical.conditions",
    "medical.blood_type",
    "medical.doctor_name",
    "medical.doctor_phone",
    "medical.insurance",
}
MEDICAL_FIELDS = {f.split(".", 1)[1] for f in PARENT_EDITABLE_FIELDS if f.startswith("medical.")}


# ---------------------------------------------------------------- logins <-> people
#
# A login never creates a Member on its own: people are added on the Members page and a
# login is linked to them explicitly (give access, or picking the person for a login).
# Parents are not members of the organization, only their children are.


def role_keys(membership) -> set[str]:
    return set(membership.role_assignments.values_list("role__key", flat=True))


def is_parent_only(membership) -> bool:
    keys = role_keys(membership)
    return not membership.is_owner and bool(keys) and keys <= {"parent"}


def ensure_guardian(membership) -> Guardian:
    guardian = Guardian.objects.filter(membership=membership).first()
    if guardian is not None:
        return guardian
    user = membership.user
    return Guardian.objects.create(
        organization=membership.organization,
        membership=membership,
        first_name=user.first_name or user.username,
        last_name=user.last_name,
        email=user.email or "",
    )


def linkable_member(organization, member_id, membership=None) -> Member:
    """A person of this organization without a login (or already linked to `membership`)."""
    member = Member.objects.select_for_update().filter(organization=organization, pk=member_id).first()
    if member is None:
        raise ValidationError({"member_id": [_("Unknown member.")]})
    if member.membership_id and (membership is None or member.membership_id != membership.pk):
        raise ValidationError({"member_id": [_("This member already has a login.")]})
    return member


def linkable_guardian(organization, guardian_id, membership=None) -> Guardian:
    """A guardian of this organization without a login (or already linked to `membership`)."""
    guardian = Guardian.objects.select_for_update().filter(organization=organization, pk=guardian_id).first()
    if guardian is None:
        raise ValidationError({"guardian_id": [_("Unknown guardian.")]})
    if guardian.membership_id and (membership is None or guardian.membership_id != membership.pk):
        raise ValidationError({"guardian_id": [_("This guardian already has a login.")]})
    return guardian


@transaction.atomic
def link_member(membership, member_id: int | None) -> Member | None:
    """Point the login at this person (or at nobody); the previous person keeps their record."""
    current = Member.objects.filter(membership=membership).first()
    member = linkable_member(membership.organization, member_id, membership) if member_id else None
    if current is not None and current != member:
        current.membership = None
        current.save(update_fields=["membership", "updated_at"])
    if member is not None and member.membership_id != membership.pk:
        member.membership = membership
        member.save(update_fields=["membership", "updated_at"])
    return member


@transaction.atomic
def link_guardian(membership, guardian_id: int | None) -> Guardian | None:
    """Point the login at this guardian (or at nobody); the guardian stays linked to their children."""
    current = Guardian.objects.filter(membership=membership).first()
    guardian = linkable_guardian(membership.organization, guardian_id, membership) if guardian_id else None
    if current is not None and current != guardian:
        current.membership = None
        current.save(update_fields=["membership", "updated_at"])
    if guardian is not None and guardian.membership_id != membership.pk:
        guardian.membership = membership
        guardian.save(update_fields=["membership", "updated_at"])
    return guardian


def replace_unit_memberships(member: Member, rows: list[dict]) -> None:
    UnitMembership.objects.filter(member=member).delete()
    UnitMembership.objects.bulk_create(
        [
            UnitMembership(member=member, unit=r["unit"], position=r.get("position", "member"), patrol=r.get("patrol"))
            for r in rows
        ]
    )
    if any(r.get("position") in LEADER_POSITIONS for r in rows) and member.kind != MemberKind.ADULT:
        member.kind = MemberKind.ADULT
        member.save(update_fields=["kind", "updated_at"])


# ---------------------------------------------------------------- visibility


def scope_members(qs: QuerySet, unit_ids: set[int] | None) -> QuerySet:
    """Members in the given units. `None` means organization-wide (everyone)."""
    if unit_ids is None:
        return qs
    return qs.filter(unit_memberships__unit_id__in=unit_ids).distinct()


def member_unit_ids(member: Member) -> set[int]:
    return set(member.unit_memberships.values_list("unit_id", flat=True))


def can_on_member(service, codename: str, member: Member) -> bool:
    """Org-wide permission, or permission on at least one of the member's units."""
    allowed = service.unit_ids(codename)
    if allowed is None:
        return service.has_perm(codename)
    return bool(allowed & member_unit_ids(member))


def missing_info_q() -> Q:
    """Active youth members without an emergency contact or a date of birth."""
    return Q(kind=MemberKind.YOUTH, status=MemberStatus.ACTIVE) & (
        Q(medical__isnull=True) | Q(medical__emergency_contacts=[]) | Q(date_of_birth__isnull=True)
    )


def missing_info_flags(member: Member) -> list[str]:
    flags = []
    if member.kind == MemberKind.YOUTH and member.status == MemberStatus.ACTIVE:
        medical = getattr(member, "medical", None)
        if medical is None or not medical.emergency_contacts:
            flags.append("emergency_contact")
        if not member.date_of_birth:
            flags.append("date_of_birth")
    return flags


# ---------------------------------------------------------------- custom fields


def fields_for(member: Member, organization) -> list[CustomField]:
    return [
        f
        for f in CustomField.objects.filter(organization=organization, is_active=True)
        if f.applies_to == AppliesTo.ALL or f.applies_to == member.kind
    ]


def clean_custom_value(field: CustomField, value):
    """Validate and normalize one custom-field value; raises ValidationError."""
    if value in (None, "", []):
        return None
    t = field.field_type
    if t == FieldType.TEXT:
        return str(value).strip()[:500]
    if t == FieldType.NUMBER:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(_("Enter a number.")) from None
        return int(number) if number.is_integer() else number
    if t == FieldType.DATE:
        parsed = value if isinstance(value, date) else parse_date(str(value))
        if parsed is None:
            raise ValidationError(_("Enter a valid date (YYYY-MM-DD)."))
        return parsed.isoformat()
    if t == FieldType.YES_NO:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"yes", "true", "1", "y", "نعم"}:
            return True
        if text in {"no", "false", "0", "n", "لا"}:
            return False
        raise ValidationError(_("Choose yes or no."))
    if t == FieldType.CHOICE:
        if value not in field.options:
            raise ValidationError(_("“%(value)s” is not one of the options.") % {"value": value})
        return value
    if t == FieldType.MULTI:
        values = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
        bad = [v for v in values if v not in field.options]
        if bad:
            raise ValidationError(_("Not an option: %(list)s") % {"list": ", ".join(bad)})
        return values
    return value


def clean_custom_data(member: Member, organization, data: dict, *, partial=True) -> dict:
    fields = {f.key: f for f in fields_for(member, organization)}
    cleaned = dict(member.custom_data or {}) if partial else {}
    errors = {}
    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            errors[key] = [_("Unknown field.")]
            continue
        try:
            cleaned[key] = clean_custom_value(field, value)
        except ValidationError as exc:
            errors[key] = exc.detail if isinstance(exc.detail, list) else [exc.detail]
    for key, field in fields.items():
        if field.required and cleaned.get(key) in (None, "", []):
            errors.setdefault(key, [_("This field is required.")])
    if errors:
        raise ValidationError({"custom_data": errors})
    return {k: v for k, v in cleaned.items() if v is not None}


# ---------------------------------------------------------------- change requests


def parent_editable_fields(member: Member, organization) -> set[str]:
    custom = {
        f"custom.{f.key}" for f in fields_for(member, organization) if f.visible_to_parents and f.editable_by_parents
    }
    return PARENT_EDITABLE_FIELDS | custom


def current_value(member: Member, field: str):
    if field.startswith("medical."):
        medical = getattr(member, "medical", None)
        return getattr(medical, field.split(".", 1)[1]) if medical else None
    if field.startswith("custom."):
        return (member.custom_data or {}).get(field.split(".", 1)[1])
    return getattr(member, field)


def apply_field(member: Member, field: str, value, actor) -> None:
    if field.startswith("medical."):
        medical, _created = MedicalInfo.objects.get_or_create(member=member)
        setattr(medical, field.split(".", 1)[1], value if value is not None else ([] if field.endswith("s") else ""))
        medical.updated_by = actor
        medical.save()
    elif field.startswith("custom."):
        key = field.split(".", 1)[1]
        data = dict(member.custom_data or {})
        if value in (None, "", []):
            data.pop(key, None)
        else:
            data[key] = value
        member.custom_data = data
        member.save(update_fields=["custom_data", "updated_at"])
    else:
        setattr(member, field, value or "")
        member.save(update_fields=[field, "updated_at"])


@transaction.atomic
def review_change_request(request_obj: ChangeRequest, *, approve_item_ids: set[int] | None, note: str, actor):
    """Approve the given items (all if None) or reject when approve_item_ids is empty."""
    items = list(request_obj.items.all())
    approved_any = False
    for item in items:
        ok = approve_item_ids is None or item.pk in approve_item_ids
        item.approved = ok
        item.save(update_fields=["approved"])
        if ok:
            apply_field(request_obj.member, item.field, item.new_value, actor)
            approved_any = True
    if not approved_any:
        status = RequestStatus.REJECTED
    elif all(i.approved for i in items):
        status = RequestStatus.APPROVED
    else:
        status = RequestStatus.PARTIAL
    request_obj.status = status
    request_obj.review_note = note
    request_obj.reviewed_by = actor
    request_obj.reviewed_at = timezone.now()
    request_obj.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at", "updated_at"])
    return request_obj
