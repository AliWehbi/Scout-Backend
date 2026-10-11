from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.organizations.models import MembershipStatus, Organization, OrganizationSettings, OrgMembership
from apps.organizations.services import create_invitation
from apps.permissions.models import Permission, PermissionGrant, RoleAssignment
from apps.permissions.service import create_default_roles
from apps.units.models import Unit, UnitMembership

DEMO_PASSWORD = "ScoutDemo!2026"

UNITS = [
    ("Cubs", "الأشبال", "Cubs", 7, 10, "#C98A2B"),
    ("Scouts", "الكشافة", "Scouts", 11, 14, "#3F7A4A"),
    ("Rovers", "الجوالة", "Rovers", 15, 18, "#A3472F"),
    ("Guides", "المرشدات", "Guides", 11, 16, "#2B6A95"),
]

# username, first, last, email, status, [(role_key, unit|None)], [(unit, position)]
PEOPLE = [
    (
        "karim.h",
        "Karim",
        "Haddad",
        "karim@cedarscouts.org",
        "active",
        [("unit_leader", "Cubs"), ("assistant_leader", "Scouts")],
        [("Cubs", "leader"), ("Scouts", "assistant")],
    ),
    ("lina.s", "Lina", "Saad", None, "active", [("assistant_leader", "Guides")], [("Guides", "assistant")]),
    (
        "nadim.f",
        "Nadim",
        "Fares",
        "nadim.fares@example.com",
        "active",
        [("unit_leader", "Rovers"), ("treasurer", None)],
        [("Rovers", "leader")],
    ),
    ("omar.t", "Omar", "Tannous", None, "active", [("scout", "Scouts")], [("Scouts", "member")]),
    ("maya.s", "Maya", "Saliba", "maya.saliba@example.com", "invited", [("parent", None)], [("Cubs", "member")]),
    (
        "joseph.a",
        "Joseph",
        "Aoun",
        "j.aoun@cedarscouts.org",
        "active",
        [("quartermaster", None)],
        [("Scouts", "member"), ("Rovers", "member")],
    ),
    ("dana.m", "Dana", "Mouawad", None, "disabled", [("scout", "Guides")], [("Guides", "member")]),
    (
        "tarek.b",
        "Tarek",
        "Bitar",
        "tarek.bitar@example.com",
        "invited",
        [("assistant_leader", "Rovers")],
        [("Rovers", "assistant"), ("Scouts", "assistant")],
    ),
]


class Command(BaseCommand):
    help = "Create a demo organization (1st Cedar Scout Group) with units, users, roles and grants."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete the demo organization and users first.")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        usernames = ["rania"] + [p[0] for p in PEOPLE]
        if reset:
            Organization.objects.filter(slug="1st-cedar-scout-group").delete()
            User.objects.filter(username__in=usernames).delete()
        if Organization.objects.filter(slug="1st-cedar-scout-group").exists():
            self.stdout.write(self.style.WARNING("Demo organization already exists (use --reset to recreate)."))
            return

        org = Organization.objects.create(
            name="1st Cedar Scout Group",
            slug="1st-cedar-scout-group",
            country="LB",
            currency="USD",
            timezone="Asia/Beirut",
        )
        OrganizationSettings.objects.create(organization=org)
        roles = create_default_roles(org)
        units = {}
        for name_en, name_ar, section, age_min, age_max, color in UNITS:
            units[name_en] = Unit.objects.create(
                organization=org,
                name_en=name_en,
                name_ar=name_ar,
                section=section,
                age_min=age_min,
                age_max=age_max,
                color=color,
            )

        rania = User.objects.create_user(
            "rania", "rania@cedarscouts.org", DEMO_PASSWORD, first_name="Rania", last_name="Khoury"
        )
        owner = OrgMembership.objects.create(organization=org, user=rania, is_owner=True)
        RoleAssignment.objects.create(membership=owner, role=roles["org_admin"], assigned_by=rania)

        memberships = {}
        for username, first, last, email, status, role_specs, unit_specs in PEOPLE:
            user = User.objects.create_user(
                username, email, DEMO_PASSWORD if status != "invited" else None, first_name=first, last_name=last
            )
            if status == "invited":
                user.set_unusable_password()
                user.save()
            m = OrgMembership.objects.create(organization=org, user=user, status=status)
            for role_key, unit in role_specs:
                RoleAssignment.objects.create(
                    membership=m, role=roles[role_key], unit=units.get(unit), assigned_by=rania
                )
            for unit, position in unit_specs:
                UnitMembership.objects.create(membership=m, unit=units[unit], position=position)
            if status == MembershipStatus.INVITED:
                create_invitation(m, rania)
            memberships[username] = m

        # Extra grants from the design: Karim manages inventory for Scouts.
        PermissionGrant.objects.create(
            membership=memberships["karim.h"],
            permission=Permission.objects.get(codename="inventory.manage"),
            unit=units["Scouts"],
            granted_by=rania,
            note="Helping the quartermaster before winter camp.",
        )

        self.stdout.write(self.style.SUCCESS("Demo organization created."))
        self.stdout.write(f"Sign in as  rania  (owner)  or  karim.h / omar.t ...  password: {DEMO_PASSWORD}")
