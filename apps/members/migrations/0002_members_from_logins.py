"""
Phase 2 data migration: every existing login that is a person in the group gets a Member,
and unit memberships move from the login to that Member. Logins that only hold the
"parent" role become Guardians (contacts with a login) instead.
"""

from django.db import migrations

ADULT_ROLE_KEYS = {"org_admin", "unit_leader", "assistant_leader", "treasurer", "quartermaster"}
LEADER_POSITIONS = {"leader", "assistant"}


def forwards(apps, schema_editor):
    OrgMembership = apps.get_model("organizations", "OrgMembership")
    Member = apps.get_model("members", "Member")
    Guardian = apps.get_model("members", "Guardian")
    UnitMembership = apps.get_model("units", "UnitMembership")
    RoleAssignment = apps.get_model("permissions", "RoleAssignment")

    for om in OrgMembership.objects.select_related("user").all():
        role_keys = set(RoleAssignment.objects.filter(membership=om).values_list("role__key", flat=True))
        unit_rows = list(UnitMembership.objects.filter(membership=om))
        user = om.user
        first = user.first_name or user.username
        if not unit_rows and not om.is_owner and role_keys and role_keys <= {"parent"}:
            Guardian.objects.create(
                organization_id=om.organization_id,
                first_name=first,
                last_name=user.last_name,
                email=user.email or "",
                membership=om,
            )
            continue
        adult = (
            om.is_owner
            or bool(role_keys & ADULT_ROLE_KEYS)
            or any(u.position in LEADER_POSITIONS for u in unit_rows)
        )
        member = Member.objects.create(
            organization_id=om.organization_id,
            membership=om,
            first_name=first,
            last_name=user.last_name,
            email=user.email or "",
            kind="adult" if adult else "youth",
            status="inactive" if om.status == "disabled" else "active",
        )
        for row in unit_rows:
            row.member = member
            row.save(update_fields=["member"])


class Migration(migrations.Migration):
    dependencies = [
        ("members", "0001_initial"),
        ("units", "0002_unitmembership_member_and_more"),
        ("permissions", "0001_initial"),
        ("organizations", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
