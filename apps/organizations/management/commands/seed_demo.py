from datetime import date, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.attendance.models import AttendanceRecord, Session
from apps.events import services as event_services
from apps.events.models import EventStatus, EventType, RespondedAs, Response
from apps.events.services import create_default_event_types
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


def arabic_name(full: str) -> dict:
    first, _sep, last = full.partition(" ")
    return {"first_name_ar": first, "last_name_ar": last.strip()}


class Command(BaseCommand):
    help = "Create a demo organization (1st Cedar Scout Group) with units, members, guardians, attendance and requests."

    def seed_events(self, org, units, youth, guardians, karim_user):
        create_default_event_types(org)
        meeting = EventType.objects.get(organization=org, name_en="Meeting")
        activity = EventType.objects.get(organization=org, name_en="Activity")
        today = date.today()
        saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)

        def event(units_, **data):
            data.setdefault("status", EventStatus.PUBLISHED)
            data.setdefault("end_date", data["start_date"])
            repeat_until = data.pop("repeat_until", None)
            created = event_services.create_event(
                organization=org,
                data=data,
                units=[units[u] for u in units_],
                leaders=[],
                repeat_until=repeat_until,
                actor=karim_user,
            )
            for e in created:
                event_services.invite(
                    e,
                    event_services.members_for_invite(org, unit_ids=[units[u].pk for u in units_]),
                    include_parents=True,
                    actor=karim_user,
                )
            return created

        def reply(e, first, response, as_=RespondedAs.PARENT):
            inv = e.invitations.get(member=youth[first])
            user = karim_user
            if as_ == RespondedAs.SELF and youth[first].membership:
                user = youth[first].membership.user
            elif as_ == RespondedAs.PARENT:
                # Parents with a login answer themselves; otherwise a leader answered for them.
                parent = next(
                    (g for g in guardians.values() if g.membership and g.links.filter(member=youth[first])), None
                )
                user, as_ = (parent.membership.user, as_) if parent else (karim_user, RespondedAs.LEADER)
            event_services.respond(inv, response, user=user, responded_as=as_)
            return inv

        cubs_meetings = event(
            ["Cubs"],
            event_type=meeting,
            title="Cubs meeting",
            start_date=saturday,
            start_time=time(10, 0),
            end_time=time(12, 30),
            location="Scout hall, Achrafieh",
            is_mandatory=True,
            reply_deadline=saturday - timedelta(days=1),
            repeat_until=saturday + timedelta(weeks=3),
        )
        for first in ("Lea", "Elie", "Maria", "Perla"):
            reply(cubs_meetings[0], first, Response.GOING)
        (guides,) = event(
            ["Guides"],
            event_type=meeting,
            title="Guides meeting",
            start_date=saturday,
            start_time=time(15, 0),
            end_time=time(17, 30),
            location="Scout hall, Achrafieh",
            is_mandatory=True,
        )
        reply(guides, "Yara", Response.GOING)
        (football,) = event(
            ["Cubs", "Scouts"],
            event_type=activity,
            title="Football vs 2nd Beirut",
            start_date=saturday + timedelta(days=1),
            start_time=time(16, 0),
            end_time=time(18, 0),
            location="Sin el Fil pitch",
            capacity=4,
        )
        for first in ("Omar", "Rami", "Elie", "Charbel", "Jad"):
            reply(football, first, Response.GOING)  # Jad lands on the waitlist
        (camp,) = event(
            ["Scouts", "Guides", "Rovers"],
            event_type=activity,
            title="Autumn camp — Ehden",
            description="Two nights under the cedars. Bring a sleeping bag, warm clothes and a torch.",
            start_date=saturday + timedelta(days=6),
            start_time=time(16, 0),
            end_date=saturday + timedelta(days=8),
            end_time=time(14, 0),
            location="Horsh Ehden campsite",
            is_mandatory=True,
            capacity=30,
            cost=45,
            reply_deadline=saturday + timedelta(days=1),
            requires_consent=True,
            consent_text=(
                "I allow my child to attend the Autumn camp in Horsh Ehden, including hiking, campfire "
                "activities and two overnight stays in tents. I confirm the medical information on file is up to date."
            ),
        )
        for first in ("Yara", "Omar", "Tala", "Rami"):
            reply(camp, first, Response.GOING)
        reply(camp, "Nour", Response.MAYBE, RespondedAs.SELF)
        for first, guardian in (("Tala", "Joseph"), ("Rami", "Tarek")):
            event_services.sign(
                camp.invitations.get(member=youth[first]),
                guardian=guardians[guardian],
                user=karim_user,
                name=guardians[guardian].full_name,
            )
        (tree,) = event(
            ["Cubs"],
            event_type=activity,
            title="Tree planting — Jaj",
            start_date=today - timedelta(days=(today.weekday() + 1) % 7 or 7),
            start_time=time(9, 0),
            end_time=time(13, 0),
            location="Jaj cedar reserve",
        )
        session = event_services.ensure_session(tree, tree.start_date, karim_user)
        for i, first in enumerate(("Jad", "Lea", "Elie", "Maria", "Charbel", "Hadi")):
            reply(tree, first, Response.GOING)
            status = "absent" if i == 4 else "present"
            AttendanceRecord.objects.create(session=session, member=youth[first], status=status)
        event(
            ["Cubs", "Scouts"],
            event_type=activity,
            title="Bake sale for the camp fund",
            start_date=saturday + timedelta(weeks=2),
            start_time=time(9, 0),
            end_time=time(13, 0),
            location="Sassine square",
            status=EventStatus.DRAFT,
        )

    def seed_inventory(self, org, units, youth, leaders, rania):
        from apps.events.models import Event
        from apps.inventory import services as inv
        from apps.inventory.models import Category, Condition, ExternalBorrower, Item, ItemLog, Location

        karim = leaders["karim.h"][0].user
        joseph = leaders["joseph.a"][0].user
        now = timezone.now()
        cats = {
            key: Category.objects.create(organization=org, name_en=en, name_ar=ar, order=i)
            for i, (key, en, ar) in enumerate(
                [
                    ("tents", "Tents", "خيم"),
                    ("cooking", "Cooking", "الطبخ"),
                    ("first_aid", "First aid", "الإسعافات"),
                    ("tools", "Tools", "أدوات"),
                ]
            )
        }
        store = Location.objects.create(organization=org, name_en="Main store", name_ar="المستودع الرئيسي")
        van = Location.objects.create(organization=org, name_en="Van", name_ar="الفان")

        def item(name_en, name_ar, kind, cat, loc, shelf="", **extra):
            obj = Item.objects.create(
                organization=org, name_en=name_en, name_ar=name_ar, kind=kind, category=cats[cat], location=loc,
                shelf=shelf, **extra,
            )  # fmt: skip
            if kind != "tracked":
                inv.log(obj, "added", joseph, quantity=obj.quantity, at=now - timedelta(days=200))
            return obj

        tent = item("Tent — 4 person", "خيمة — 4 أشخاص", "tracked", "tents", store, "Shelf B", code_prefix="TNT",
                    brand="Vango Tempest 400", unit_cost=180, purchase_date=date(2022, 3, 1),
                    notes="Pack poles in the green bag. Air dry before storing.")  # fmt: skip
        inv.add_pieces(tent, 8, joseph, condition=Condition.GOOD)
        stove = item("Patrol stove", "موقد الطليعة", "tracked", "cooking", store, "Shelf C", code_prefix="STV")
        inv.add_pieces(stove, 4, joseph, condition=Condition.GOOD)
        kit = item("First-aid kit", "حقيبة إسعافات", "tracked", "first_aid", van, code_prefix="FAK")
        inv.add_pieces(kit, 3, joseph, condition=Condition.GOOD)
        tins = item("Mess tins", "صحون معدنية", "counted", "cooking", store, "Box 4", quantity=40)
        rope = item("Rope — 20 m", "حبل — 20 م", "counted", "tools", store, "Shelf A", quantity=10)
        item("Ground sheets", "أغطية أرضية", "counted", "tents", store, "Shelf B", quantity=12)
        item("Plasters (box)", "لصقات جروح (علبة)", "consumable", "first_aid", van, quantity=3, min_stock=5)
        gas = item("Gas canisters", "عبوات غاز", "consumable", "cooking", store, "Cage", quantity=6, min_stock=10)

        # The store existed long before today: date the "added" history in the past.
        ItemLog.objects.filter(item__organization=org, kind="added").update(at=now - timedelta(days=200))
        tents = list(tent.pieces.all())
        inv.set_piece_condition(tents[5], Condition.UNDER_REPAIR, actor=leaders["nadim.f"][0].user, note="Broken pole")
        tents[2].condition = Condition.FAIR
        tents[2].save(update_fields=["condition"])
        today = timezone.localdate()
        inv.check_out(
            org,
            borrower={"borrower_type": "member", "member": youth["Rami"], "due_date": today - timedelta(days=2)},
            lines=[{"item": tent, "pieces": [tents[1]]}],
            note="Family camping weekend",
            actor=karim,
            at=now - timedelta(days=9),
        )
        beirut = ExternalBorrower.objects.create(
            organization=org, name="2nd Beirut Scout Group", contact_person="Fadi Nasr", phone="+961 3 448 120"
        )
        inv.check_out(
            org,
            borrower={"borrower_type": "external", "external": beirut, "due_date": today - timedelta(days=1)},
            lines=[{"item": rope, "quantity": 3}, {"item": stove, "pieces": [list(stove.pieces.all())[3]]}],
            note="For their autumn camp in Faraya",
            actor=joseph,
            at=now - timedelta(days=11),
        )
        neighbour = ExternalBorrower.objects.create(
            organization=org, name="Hanna Khoury", contact_person="", phone="+961 70 112 334", notes="Neighbour"
        )
        inv.check_out(
            org,
            borrower={"borrower_type": "external", "external": neighbour, "due_date": today + timedelta(days=2)},
            lines=[{"item": tent, "pieces": [tents[4]]}],
            note="Family trip",
            actor=rania,
            at=now - timedelta(days=3),
        )
        sheets = Item.objects.get(organization=org, name_en="Ground sheets")
        inv.check_out(
            org,
            borrower={"borrower_type": "member", "member": youth["Yara"], "due_date": today + timedelta(days=5)},
            lines=[{"item": sheets, "quantity": 1}],
            note="Guides patrol overnight",
            actor=leaders["lina.s"][0].user,
            at=now - timedelta(days=1),
        )
        inv.check_out(
            org,
            borrower={"borrower_type": "unit", "unit": units["Cubs"], "due_date": None},
            lines=[{"item": tins, "quantity": 12}],
            note="For weekly meetings",
            actor=karim,
            at=now - timedelta(days=7),
        )
        # A returned loan, so the history shows who checked it back in.
        loan, _w = inv.check_out(
            org,
            borrower={"borrower_type": "unit", "unit": units["Scouts"], "due_date": today - timedelta(days=1)},
            lines=[{"item": tent, "pieces": [tents[3]]}, {"item": gas, "quantity": 2}],
            note="Jaj hike",
            actor=karim,
            at=now - timedelta(days=4),
        )
        inv.return_lines(
            loan,
            [{"line": loan.lines.get(piece=tents[3]), "returned": 1, "condition": "good"}],
            returned_by_name="Omar Tannous",
            returned_at=now - timedelta(days=1, hours=2),
            actor=karim,
        )
        inv.adjust_stock(gas, "restock", 4, actor=joseph, note="Bought at Spinneys")
        ItemLog.objects.filter(item=gas, kind="restocked").update(at=now - timedelta(days=1, hours=5))
        ItemLog.objects.filter(item=tent, kind="repair").update(at=now - timedelta(days=15))
        camp = Event.objects.filter(organization=org, title__startswith="Autumn camp").first()
        if camp:
            inv.reserve(camp, tent, pieces=[tents[2], tents[3]], actor=rania)
            inv.reserve(camp, stove, quantity=3, actor=rania)
            inv.reserve(camp, tins, quantity=20, actor=rania)
            inv.reserve(camp, kit, pieces=[list(kit.pieces.all())[1]], actor=rania)
            inv.reserve(camp, gas, quantity=4, actor=rania)

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
            first_name_ar="رانيا",
            last_name_ar="خوري",
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
                **arabic_name(name_ar),
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
                **arabic_name(name_ar),
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

        self.seed_events(org, units, youth, guardians, karim.membership.user)
        self.seed_inventory(org, units, youth, leaders, rania)

        self.stdout.write(self.style.SUCCESS("Demo organization created."))
        self.stdout.write(
            f"Sign in as rania (owner), karim.h (Cubs leader) or maya.s (parent). Password: {DEMO_PASSWORD}"
        )
