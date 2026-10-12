from datetime import date, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.attendance.models import AttendanceRecord, Session
from apps.members.models import (
    Certification,
    ChangeRequest,
    ChangeRequestItem,
    CustomField,
    Guardian,
    GuardianLink,
    MedicalInfo,
    Member,
)
from apps.organizations.models import (
    MembershipStatus,
    Organization,
    OrganizationSettings,
    OrgMembership,
)
from apps.organizations.services import create_invitation
from apps.permissions.models import Permission, PermissionGrant, RoleAssignment
from apps.permissions.service import create_default_roles
from apps.units.models import Patrol, Unit, UnitMembership

DEMO_PASSWORD = "ScoutDemo!2026"
SLUG = "1st-cedar-scout-group"

UNITS = [
    ("Cubs", "الأشبال", "Cubs", 7, 10, "#C98A2B", ["Red", "Blue", "Green", "Yellow"]),
    ("Scouts", "الكشافة", "Scouts", 11, 14, "#3F7A4A", ["Eagles", "Wolves"]),
    ("Rovers", "الجوالة", "Rovers", 15, 18, "#A3472F", []),
    ("Guides", "المرشدات", "Guides", 11, 16, "#2B6A95", ["Lilies", "Roses"]),
]

# Logins: username, first, last, email, login status, [(role, unit|None)], [(unit, position)], name_ar
LEADERS = [
    (
        "karim.h",
        "Karim",
        "Haddad",
        "karim@cedarscouts.org",
        "active",
        [("unit_leader", "Cubs"), ("assistant_leader", "Scouts")],
        [("Cubs", "leader"), ("Scouts", "assistant")],
        "كريم حداد",
    ),
    ("lina.s", "Lina", "Saad", None, "active", [("unit_leader", "Guides")], [("Guides", "leader")], "لينا سعد"),
    (
        "nadim.f",
        "Nadim",
        "Fares",
        "nadim.fares@example.com",
        "active",
        [("unit_leader", "Rovers"), ("treasurer", None)],
        [("Rovers", "leader")],
        "نديم فارس",
    ),
    (
        "joseph.a",
        "Joseph",
        "Aoun",
        "j.aoun@cedarscouts.org",
        "active",
        [("quartermaster", None)],
        [("Scouts", "leader")],
        "جوزيف عون",
    ),
    (
        "tarek.b",
        "Tarek",
        "Bitar",
        "tarek.bitar@example.com",
        "invited",
        [("assistant_leader", "Rovers")],
        [("Rovers", "assistant")],
        "طارق بيطار",
    ),
]

# Youth: first, last, name_ar, dob, gender, unit, patrol, custom, has_login (username)
YOUTH = [
    (
        "Jad",
        "Saliba",
        "جاد صليبا",
        date(2017, 3, 14),
        "male",
        "Cubs",
        "Red",
        {"school": "Collège Notre-Dame", "uniform_size": "S", "can_swim": True},
        None,
    ),
    ("Lea", "Haddad", "ليا حداد", date(2018, 5, 2), "female", "Cubs", "Red", {"uniform_size": "XS"}, None),
    ("Elie", "Mansour", "إيلي منصور", date(2017, 6, 2), "male", "Cubs", "Blue", {}, None),
    ("Maria", "Khalil", "ماريا خليل", date(2016, 9, 9), "female", "Cubs", "Blue", {"can_swim": False}, None),
    ("Charbel", "Daher", "شربل ضاهر", date(2016, 11, 19), "male", "Cubs", "Green", {}, None),
    ("Yasmine", "Rizk", "ياسمين رزق", date(2017, 8, 7), "female", "Cubs", "Green", {}, None),
    ("Hadi", "Nassar", "هادي نصار", date(2016, 12, 12), "male", "Cubs", "Yellow", {}, None),
    ("Perla", "Azar", "بيرلا عازار", date(2017, 1, 25), "female", "Cubs", "Yellow", {}, None),
    ("Omar", "Tannous", "عمر طانوس", date(2014, 2, 3), "male", "Scouts", "Eagles", {"uniform_size": "M"}, "omar.t"),
    ("Rami", "Bitar", "رامي بيطار", date(2015, 4, 21), "male", "Scouts", "Wolves", {}, None),
    ("Tala", "Aoun", "تالا عون", date(2013, 7, 30), "female", "Guides", "Lilies", {}, None),
    (
        "Yara",
        "Saliba",
        "يارا صليبا",
        date(2013, 1, 8),
        "female",
        "Guides",
        "Roses",
        {"school": "Collège Notre-Dame"},
        None,
    ),
    ("Nour", "Fares", "نور فارس", date(2010, 10, 10), "female", "Rovers", None, {"can_swim": True}, "nour.f"),
]

GUARDIANS = [
    # first, last, phone, email, login username, [(child first, relationship, primary)]
    (
        "Maya",
        "Saliba",
        "+961 3 123 456",
        "maya.saliba@example.com",
        "maya.s",
        [("Jad", "mother", True), ("Yara", "mother", True)],
    ),
    ("Fadi", "Saliba", "+961 70 555 210", "", None, [("Jad", "father", False), ("Yara", "father", False)]),
    ("Rima", "Haddad", "+961 3 445 900", "rima@example.com", None, [("Lea", "mother", True)]),
    ("George", "Tannous", "+961 71 222 333", "", None, [("Omar", "father", True)]),
    ("Tarek", "Bitar", "+961 3 777 000", "", None, [("Rami", "father", True)]),
    ("Joseph", "Aoun", "+961 76 101 202", "", None, [("Tala", "father", True)]),
    ("Nadim", "Fares", "+961 3 909 101", "", None, [("Nour", "father", True)]),
    ("Rita", "Mansour", "+961 3 220 118", "", None, [("Elie", "mother", True)]),
]

CUSTOM_FIELDS = [
    ("school", "School", "المدرسة", "text", [], "youth", "additional", False, True, True),
    (
        "uniform_size",
        "Uniform size",
        "مقاس الزي",
        "choice",
        ["XS", "S", "M", "L", "XL"],
        "youth",
        "additional",
        False,
        True,
        True,
    ),
    ("can_swim", "Can swim", "يجيد السباحة", "yesno", [], "all", "additional", False, True, True),
    ("scarf_received", "Scarf received", "استلام المنديل", "date", [], "youth", "additional", False, True, False),
    ("dietary_needs", "Dietary needs", "النظام الغذائي", "text", [], "all", "medical", False, True, True),
    (
        "safeguarding_no",
        "Safeguarding check no.",
        "رقم شهادة الحماية",
        "text",
        [],
        "adult",
        "additional",
        False,
        False,
        False,
    ),
]


class Command(BaseCommand):
    help = "Create a demo organization (1st Cedar Scout Group) with units, members, guardians, attendance and requests."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete the demo organization and its users first.")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        usernames = ["rania", "omar.t", "nour.f", "maya.s"] + [p[0] for p in LEADERS]
        if reset:
            Organization.objects.filter(slug=SLUG).delete()
            User.objects.filter(username__in=usernames).delete()
        if Organization.objects.filter(slug=SLUG).exists():
            self.stdout.write(self.style.WARNING("Demo organization already exists (use --reset to recreate)."))
            return

        org = Organization.objects.create(
            name="1st Cedar Scout Group", slug=SLUG, country="LB", currency="USD", timezone="Asia/Beirut"
        )
        OrganizationSettings.objects.create(organization=org)
        roles = create_default_roles(org)
        units, patrols = {}, {}
        for name_en, name_ar, section, age_min, age_max, color, patrol_names in UNITS:
            unit = Unit.objects.create(
                organization=org,
                name_en=name_en,
                name_ar=name_ar,
                section=section,
                age_min=age_min,
                age_max=age_max,
                color=color,
            )
            units[name_en] = unit
            for p in patrol_names:
                patrols[(name_en, p)] = Patrol.objects.create(organization=org, unit=unit, name=p)
        for i, (key, en, ar, ftype, options, applies, section, required, visible, editable) in enumerate(CUSTOM_FIELDS):
            CustomField.objects.create(
                organization=org,
                key=key,
                label_en=en,
                label_ar=ar,
                field_type=ftype,
                options=options,
                applies_to=applies,
                section=section,
                required=required,
                visible_to_parents=visible,
                editable_by_parents=editable,
                order=i,
            )

        def login(username, first, last, email, status=MembershipStatus.ACTIVE, password=True):
            user = User.objects.create_user(
                username, email, DEMO_PASSWORD if password else None, first_name=first, last_name=last
            )
            if not password:
                user.set_unusable_password()
                user.save()
            return OrgMembership.objects.create(organization=org, user=user, status=status)

        rania_login = login("rania", "Rania", "Khoury", "rania@cedarscouts.org")
        rania_login.is_owner = True
        rania_login.save()
        rania = rania_login.user
        RoleAssignment.objects.create(membership=rania_login, role=roles["org_admin"], assigned_by=rania)
        Member.objects.create(
            organization=org,
            membership=rania_login,
            first_name="Rania",
            last_name="Khoury",
            name_ar="رانيا خوري",
            kind="adult",
            email="rania@cedarscouts.org",
        )

        leaders = {}
        for username, first, last, email, status, role_specs, unit_specs, name_ar in LEADERS:
            m = login(username, first, last, email, status, password=status != "invited")
            for role_key, unit in role_specs:
                RoleAssignment.objects.create(
                    membership=m, role=roles[role_key], unit=units.get(unit), assigned_by=rania
                )
            person = Member.objects.create(
                organization=org,
                membership=m,
                first_name=first,
                last_name=last,
                name_ar=name_ar,
                kind="adult",
                email=email or "",
                join_date=date(2022, 9, 1),
            )
            for unit, position in unit_specs:
                UnitMembership.objects.create(member=person, unit=units[unit], position=position)
            if status == "invited":
                create_invitation(m, rania)
            leaders[username] = (m, person)
        karim_login, karim = leaders["karim.h"]
        PermissionGrant.objects.create(
            membership=karim_login,
            permission=Permission.objects.get(codename="inventory.manage"),
            unit=units["Scouts"],
            granted_by=rania,
            note="Helping the quartermaster before winter camp.",
        )
        Certification.objects.create(
            member=karim, name="First aid", issued_on=date(2023, 11, 2), expires_on=date.today() + timedelta(days=24)
        )
        Certification.objects.create(
            member=karim, name="Safeguarding", issued_on=date(2024, 3, 1), expires_on=date(2027, 3, 1)
        )
        Certification.objects.create(
            member=leaders["lina.s"][1],
            name="First aid",
            issued_on=date(2022, 6, 1),
            expires_on=date.today() - timedelta(days=10),
        )

        youth = {}
        for first, last, name_ar, dob, gender, unit, patrol, custom, username in YOUTH:
            m_login = None
            if username:
                m_login = login(username, first, last, None)
                RoleAssignment.objects.create(
                    membership=m_login, role=roles["scout"], unit=units[unit], assigned_by=rania
                )
            person = Member.objects.create(
                organization=org,
                membership=m_login,
                first_name=first,
                last_name=last,
                name_ar=name_ar,
                date_of_birth=dob,
                gender=gender,
                kind="youth",
                join_date=date(2024, 9, 14),
                custom_data=custom,
                address="Achrafieh, Beirut" if last == "Saliba" else "",
            )
            UnitMembership.objects.create(
                member=person,
                unit=units[unit],
                position="patrol_leader" if first == "Omar" else "member",
                patrol=patrols.get((unit, patrol)),
            )
            youth[first] = person

        guardians = {}
        for first, last, phone, email, username, children in GUARDIANS:
            g_login = None
            if username:
                g_login = login(username, first, last, email)
                RoleAssignment.objects.create(membership=g_login, role=roles["parent"], assigned_by=rania)
            guardian = Guardian.objects.create(
                organization=org, first_name=first, last_name=last, phone=phone, email=email, membership=g_login
            )
            for child, relationship, primary in children:
                GuardianLink.objects.create(
                    member=youth[child], guardian=guardian, relationship=relationship, is_primary=primary
                )
            guardians[first] = guardian

        MedicalInfo.objects.create(
            member=youth["Jad"],
            allergies=[
                {
                    "name": "Peanuts",
                    "severity": "severe",
                    "notes": "Anaphylaxis risk — EpiPen always in the first-aid kit",
                },
                {"name": "Pollen", "severity": "mild", "notes": "Seasonal, spring camps"},
            ],
            medications=[
                {"name": "EpiPen Jr 0.15 mg", "dose": "1 injection", "when": "Emergency — allergic reaction"},
                {"name": "Cetirizine 5 mg", "dose": "1 tablet", "when": "Daily in spring, evening"},
            ],
            emergency_contacts=[
                {"name": "Maya Saliba", "relationship": "Mother", "phone": "+961 3 123 456"},
                {"name": "Fadi Saliba", "relationship": "Father", "phone": "+961 70 555 210"},
                {"name": "Nadia Saliba", "relationship": "Grandmother", "phone": "+961 1 202 303"},
            ],
            blood_type="A+",
            doctor_name="Dr. Hanna Abi Nader",
            doctor_phone="+961 1 333 444",
            insurance="AXA Middle East · #4471-22",
            updated_by=karim.membership.user,
        )
        for first in ("Elie", "Maria", "Charbel", "Yasmine", "Perla", "Omar", "Tala", "Yara", "Nour"):
            MedicalInfo.objects.create(
                member=youth[first],
                emergency_contacts=[
                    {"name": f"Parent of {first}", "relationship": "Parent", "phone": "+961 3 000 000"}
                ],
            )
        # Lea, Hadi and Rami have no emergency contact -> "missing info".

        change = ChangeRequest.objects.create(
            organization=org,
            member=youth["Jad"],
            requested_by=guardians["Maya"].membership.user,
            guardian=guardians["Maya"],
            message="Jad was diagnosed with a sesame allergy last week, and we changed our home number.",
        )
        jad_allergies = youth["Jad"].medical.allergies
        ChangeRequestItem.objects.create(
            request=change,
            field="medical.allergies",
            old_value=jad_allergies,
            new_value=jad_allergies + [{"name": "Sesame", "severity": "severe", "notes": ""}],
        )
        contacts = youth["Jad"].medical.emergency_contacts
        new_contacts = [dict(c) for c in contacts]
        new_contacts[1]["phone"] = "+961 70 555 219"
        ChangeRequestItem.objects.create(
            request=change, field="medical.emergency_contacts", old_value=contacts, new_value=new_contacts
        )
        ChangeRequestItem.objects.create(request=change, field="custom.uniform_size", old_value="S", new_value="M")

        cubs = [youth[n] for n in ("Jad", "Lea", "Elie", "Maria", "Charbel", "Yasmine", "Hadi", "Perla")]
        pattern = ["present"] * 6 + ["absent", "excused"]
        today = date.today()
        for weeks in range(8, 0, -1):
            session = Session.objects.create(
                organization=org,
                unit=units["Cubs"],
                title="Saturday meeting",
                date=today - timedelta(weeks=weeks),
                start_time=time(10, 0),
                end_time=time(12, 30),
                location="Scout hall, Achrafieh",
                created_by=karim.membership.user,
            )
            for i, member in enumerate(cubs):
                AttendanceRecord.objects.create(
                    session=session, member=member, status=pattern[(i + weeks) % len(pattern)]
                )

        self.stdout.write(self.style.SUCCESS("Demo organization created."))
        self.stdout.write(
            f"Sign in as rania (owner), karim.h (Cubs leader) or maya.s (parent). Password: {DEMO_PASSWORD}"
        )
