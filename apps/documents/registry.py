"""
What each kind of document can show. This is the contract with uploaded templates:

* `FIELDS` lists every placeholder (shown in Settings, used to reject unknown fields).
* `build_context` turns an event into plain dicts/lists/strings in the chosen language.
  Never pass model instances: templates must not be able to walk to other data.
* `sample_context` fills the same shape with made-up data to test an upload.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.utils import formats, timezone, translation

from apps.customfields import services as custom_field_services
from apps.customfields.models import CustomField
from apps.events import services as event_services
from apps.events.models import Event
from apps.members.models import GuardianLink

from .models import EXCEL_KINDS, DocumentKind

LANGS = ("en", "ar")

# Labels for stored values, in both languages (the backend has no Arabic message catalog).
LABELS = {
    "response": {
        "going": ("Going", "سيحضر"),
        "maybe": ("Maybe", "ربما"),
        "not_going": ("Not going", "لن يحضر"),
        "": ("No reply", "لم يرد"),
    },
    "attendance": {
        "present": ("Present", "حاضر"),
        "absent": ("Absent", "غائب"),
        "excused": ("Excused", "معذور"),
        None: ("", ""),
    },
    "relationship": {
        "mother": ("Mother", "الأم"),
        "father": ("Father", "الأب"),
        "guardian": ("Guardian", "ولي الأمر"),
        "grandparent": ("Grandparent", "الجد/الجدة"),
        "sibling": ("Sibling", "الأخ/الأخت"),
        "other": ("Other", "آخر"),
    },
    "status": {
        "draft": ("Draft", "مسودة"),
        "published": ("Published", "منشور"),
        "cancelled": ("Cancelled", "ملغى"),
    },
    "position": {
        "member": ("Member", "عضو"),
        "leader": ("Leader", "قائد"),
        "assistant": ("Assistant leader", "قائد مساعد"),
        "patrol_leader": ("Patrol leader", "رئيس طليعة"),
    },
    "yes_no": {True: ("Yes", "نعم"), False: ("No", "لا")},
}


def label(group: str, value, lang: str) -> str:
    pair = LABELS[group].get(value)
    if pair is None:
        return str(value or "")
    return pair[1] if lang == "ar" else pair[0]


# ------------------------------------------------------------------ field catalog
# (name, English label, Arabic label). Lists carry the loop variable and their item fields.


def f(name, en, ar):
    return {"name": name, "en": en, "ar": ar}


def loop(name, var, en, ar, items):
    return {"name": name, "var": var, "en": en, "ar": ar, "items": items}


COMMON_FIELDS = [
    f("org.name", "Organization name", "اسم المنظمة"),
    f("org.logo", "Organization logo (image)", "شعار المنظمة (صورة)"),
    f("event.title", "Event title", "عنوان النشاط"),
    f("event.description", "Description", "الوصف"),
    f("event.type_en", "Event type (English)", "نوع النشاط (إنجليزي)"),
    f("event.type_ar", "Event type (Arabic)", "نوع النشاط (عربي)"),
    f("event.status", "Status", "الحالة"),
    f("event.start_date", "Start date", "تاريخ البدء"),
    f("event.start_time", "Start time", "وقت البدء"),
    f("event.end_date", "End date", "تاريخ الانتهاء"),
    f("event.end_time", "End time", "وقت الانتهاء"),
    f("event.location", "Location", "المكان"),
    f("event.cost", "Cost", "التكلفة"),
    f("event.currency", "Currency", "العملة"),
    f("event.reply_deadline", "Reply by", "آخر موعد للرد"),
    f("event.is_mandatory_text", "Mandatory (Yes/No)", "إلزامي (نعم/لا)"),
    f("event.requires_consent_text", "Needs parent consent (Yes/No)", "يتطلب موافقة الأهل (نعم/لا)"),
    f("event.consent_text", "Consent text", "نص الموافقة"),
    f("event.unit_names_en", "Units (English, comma separated)", "الوحدات (إنجليزي)"),
    f("event.unit_names_ar", "Units (Arabic, comma separated)", "الوحدات (عربي)"),
    f("event.leader_names", "Leaders (comma separated)", "القادة"),
    f("event.leader_names_ar", "Leaders in Arabic (comma separated)", "القادة (عربي)"),
    loop(
        "event.units",
        "u",
        "Each unit",
        "كل وحدة",
        [
            f("u.name_en", "Unit name (English)", "اسم الوحدة (إنجليزي)"),
            f("u.name_ar", "Unit name (Arabic)", "اسم الوحدة (عربي)"),
        ],
    ),
    loop(
        "event.leaders",
        "l",
        "Each leader",
        "كل قائد",
        [f("l.full_name", "Name", "الاسم"), f("l.name_ar", "Name in Arabic", "الاسم بالعربية")],
    ),
    f("generated_at", "Date and time of export", "تاريخ ووقت التصدير"),
    f("generated_by", "Exported by", "صدّره"),
]

MEMBER_ITEM = [
    ("n", "Row number", "الرقم"),
    ("full_name", "Name", "الاسم"),
    ("name_ar", "Name in Arabic", "الاسم بالعربية"),
]

KIND_FIELDS = {
    DocumentKind.EVENT_SHEET: [
        f("counts.invited", "Number invited", "عدد المدعوين"),
        f("counts.going", "Number going", "عدد الحضور المؤكد"),
        f("counts.maybe", "Number maybe", "عدد ربما"),
        f("counts.not_going", "Number not going", "عدد المعتذرين"),
        f("counts.no_reply", "Number without reply", "عدد من لم يرد"),
        f("counts.waitlist", "Number on the waitlist", "عدد قائمة الانتظار"),
        f("counts.signed", "Number of signed consents", "عدد الموافقات الموقعة"),
        loop(
            "participants",
            "p",
            "Each invited member",
            "كل عضو مدعو",
            [f(f"p.{k}", en, ar) for k, en, ar in MEMBER_ITEM]
            + [
                f("p.unit", "Unit", "الوحدة"),
                f("p.response", "Reply", "الرد"),
                f("p.waitlisted_text", "On waitlist (Yes/No)", "على قائمة الانتظار (نعم/لا)"),
                f("p.signed_text", "Consent signed (Yes/No)", "تم توقيع الموافقة (نعم/لا)"),
            ],
        ),
    ],
    DocumentKind.ATTENDANCE_SHEET: [
        f("day", "Day of the sheet", "اليوم"),
        f("counts.present", "Number present", "عدد الحاضرين"),
        f("counts.absent", "Number absent", "عدد الغائبين"),
        f("counts.excused", "Number excused", "عدد المعذورين"),
        f("counts.unmarked", "Number not marked yet", "عدد غير المسجلين"),
        loop(
            "rows",
            "r",
            "Each member on the roster",
            "كل عضو في القائمة",
            [f(f"r.{k}", en, ar) for k, en, ar in MEMBER_ITEM]
            + [
                f("r.position", "Position", "المنصب"),
                f("r.patrol", "Patrol", "الطليعة"),
                f("r.response", "Reply", "الرد"),
                f("r.status", "Attendance (if taken)", "الحضور (إن سُجّل)"),
                f("r.note", "Note", "ملاحظة"),
            ],
        ),
    ],
    DocumentKind.CONSENT_SLIPS: [
        loop(
            "slips",
            "s",
            "Each slip (one per member, put a page break inside the loop)",
            "كل قسيمة (واحدة لكل عضو، ضع فاصل صفحة داخل الحلقة)",
            [f(f"s.{k}", en, ar) for k, en, ar in MEMBER_ITEM]
            + [
                f("s.guardian_names", "Parents/guardians (comma separated)", "الأهل/أولياء الأمور"),
                f("s.consent_text", "Consent text", "نص الموافقة"),
                f("s.signed_text", "Signed (Yes/No)", "موقّعة (نعم/لا)"),
                f("s.signed_by", "Signed by", "وقّعها"),
                f("s.signed_at", "Signed on", "تاريخ التوقيع"),
                loop(
                    "s.guardians",
                    "g",
                    "Each parent/guardian",
                    "كل ولي أمر",
                    [
                        f("g.full_name", "Name", "الاسم"),
                        f("g.relationship", "Relationship", "صلة القرابة"),
                        f("g.phone", "Phone", "الهاتف"),
                    ],
                ),
            ],
        ),
    ],
}


# Custom fields that go into the event documents: the event's own and the organization's.
DOCUMENT_ENTITIES = ("event", "organization")


def coded_fields(organization, entities=DOCUMENT_ENTITIES) -> list[CustomField]:
    """The organization's active custom fields with a template code, for these kinds of record."""
    if organization is None:
        return []
    fields = []
    for entity in entities:
        fields += [f for f in custom_field_services.active_fields(organization, entity) if f.code]
    return fields


def coded_event_fields(organization) -> list[CustomField]:
    return coded_fields(organization, ("event",))


def custom_items(organization) -> list[dict]:
    """The organization's own placeholders, e.g. event.cedar_meeting_point or org.cedar_phone."""
    items = []
    for field in coded_fields(organization):
        items.append(
            {
                "name": field.template_code(organization.code),
                "en": field.label_en,
                "ar": field.label_ar or field.label_en,
                "custom": True,
                "entity": field.entity,
                "event_types": [{"en": t.name_en, "ar": t.name_ar or t.name_en} for t in field.event_types.all()],
            }
        )
    return items


def field_value(field: CustomField, event: Event):
    """(value, source) of a coded field for this event's documents."""
    if field.entity == "organization":
        return field.default, ("organization" if field.default not in custom_field_services.EMPTY else "empty")
    return custom_field_services.value_or_default(field, event)


# Excel lists: codes are replaced as plain text; {{ table }} marks where the data starts.
LIST_FIELDS = [
    f("table", "Where the list starts (alone in its cell)", "مكان بداية القائمة (وحده في الخلية)"),
    f("org.name", "Organization name", "اسم المنظمة"),
    f("org.logo", "Organization logo (alone in its cell)", "شعار المنظمة (وحده في الخلية)"),
    f("generated_at", "Date and time of export", "تاريخ ووقت التصدير"),
    f("generated_by", "Exported by", "صدّره"),
    f("count", "Number of rows in the list", "عدد الصفوف في القائمة"),
]


def fields_for(kind: str, organization=None) -> list[dict]:
    if kind in EXCEL_KINDS:
        # Lists aren't about one event: only the organization's own fields apply.
        org_items = [i for i in custom_items(organization) if i["entity"] == "organization"]
        return org_items + LIST_FIELDS
    return custom_items(organization) + COMMON_FIELDS + KIND_FIELDS[kind]


def list_codes(organization) -> set[str]:
    return {item["name"] for item in fields_for(DocumentKind.MEMBERS_LIST, organization)}


def list_values(organization, *, lang: str, user, count: int, overrides: dict | None = None) -> dict[str, str]:
    """Text for each code of an Excel list ({{ table }} and {{ org.logo }} are handled apart)."""
    values = {
        "org.name": organization.name,
        "generated_at": fmt_datetime(timezone.now(), lang, organization.timezone),
        "generated_by": user.full_name if user else "",
        "count": str(count),
    }
    for field in coded_fields(organization, ("organization",)):
        code = field.template_code(organization.code)
        value = overrides[code] if overrides and code in overrides else field.default
        values[code] = custom_field_services.display(field, value, lang)
    return values


def allowed_roots(kind: str) -> frozenset[str]:
    return frozenset(item["name"].split(".")[0] for item in fields_for(kind))


# ------------------------------------------------------------------ formatting


def fmt_date(value: date | None, lang: str) -> str:
    if value is None:
        return ""
    with translation.override(lang):
        return formats.date_format(value, "j F Y")


def fmt_datetime(value: datetime | None, lang: str, tz: str) -> str:
    if value is None:
        return ""
    try:
        zone = ZoneInfo(tz)
    except (ValueError, KeyError):
        zone = ZoneInfo("UTC")
    local = timezone.localtime(value, zone)
    return f"{fmt_date(local.date(), lang)} {local:%H:%M}"


def fmt_time(value) -> str:
    return value.strftime("%H:%M") if value else ""


def join(names, lang: str) -> str:
    return ("، " if lang == "ar" else ", ").join(n for n in names if n)


# ------------------------------------------------------------------ real data


def _common(event: Event, *, lang: str, user) -> dict:
    org = event.organization
    units = list(event.units.all())
    leaders = list(event.leaders.all())
    return {
        "org": {"name": org.name, "logo": ""},
        "event": {
            "title": event.title,
            "description": event.description,
            "type_en": event.event_type.name_en,
            "type_ar": event.event_type.name_ar or event.event_type.name_en,
            "status": label("status", event.status, lang),
            "start_date": fmt_date(event.start_date, lang),
            "start_time": fmt_time(event.start_time),
            "end_date": fmt_date(event.end_date, lang),
            "end_time": fmt_time(event.end_time),
            "location": event.location,
            "cost": f"{event.cost:.2f}" if event.cost is not None else "",
            "currency": org.currency,
            "reply_deadline": fmt_date(event.reply_deadline, lang),
            "is_mandatory": event.is_mandatory,
            "is_mandatory_text": label("yes_no", event.is_mandatory, lang),
            "requires_consent": event.requires_consent,
            "requires_consent_text": label("yes_no", event.requires_consent, lang),
            "consent_text": event.consent_text,
            "units": [{"name_en": u.name_en, "name_ar": u.name_ar or u.name_en} for u in units],
            "unit_names_en": join([u.name_en for u in units], "en"),
            "unit_names_ar": join([u.name_ar or u.name_en for u in units], "ar"),
            "leaders": [{"full_name": m.full_name, "name_ar": m.name_ar or m.full_name} for m in leaders],
            "leader_names": join([m.full_name for m in leaders], lang),
            "leader_names_ar": join([m.name_ar or m.full_name for m in leaders], "ar"),
        },
        "generated_at": fmt_datetime(timezone.now(), lang, org.timezone),
        "generated_by": user.full_name if user else "",
    }


def _member_names(member) -> dict:
    return {"full_name": member.full_name, "name_ar": member.name_ar or member.full_name}


def _invitations(event: Event):
    return list(
        event.invitations.select_related("member")
        .prefetch_related("signatures", "member__unit_memberships__unit")
        .order_by("member__first_name", "member__last_name", "pk")
    )


def _event_sheet(event: Event, lang: str) -> dict:
    rows, counts = [], dict.fromkeys(("invited", "going", "maybe", "not_going", "no_reply", "waitlist", "signed"), 0)
    event_units = set(event.units.values_list("pk", flat=True))
    for n, inv in enumerate(_invitations(event), start=1):
        signed = inv.valid_signature is not None
        memberships = [um for um in inv.member.unit_memberships.all() if not event_units or um.unit_id in event_units]
        unit = memberships[0].unit if memberships else None
        rows.append(
            {
                "n": n,
                **_member_names(inv.member),
                "unit": (unit.name_ar or unit.name_en if lang == "ar" else unit.name_en) if unit else "",
                "response": label("response", inv.response, lang),
                "waitlisted": inv.waitlisted,
                "waitlisted_text": label("yes_no", inv.waitlisted, lang),
                "signed": signed,
                "signed_text": label("yes_no", signed, lang) if event.requires_consent else "",
            }
        )
        counts["invited"] += 1
        counts["waitlist"] += inv.waitlisted
        if inv.response == "going" and not inv.waitlisted:
            counts["going"] += 1
            counts["signed"] += signed
        elif inv.response == "maybe":
            counts["maybe"] += 1
        elif inv.response == "not_going":
            counts["not_going"] += 1
        elif not inv.response:
            counts["no_reply"] += 1
    return {"participants": rows, "counts": counts}


def _attendance_sheet(event: Event, lang: str, day: date) -> dict:
    _session, roster = event_services.event_roster(event, day)
    counts = {"present": 0, "absent": 0, "excused": 0, "unmarked": 0}
    rows = []
    for n, row in enumerate(roster, start=1):
        counts[row["status"] or "unmarked"] += 1
        rows.append(
            {
                "n": n,
                "full_name": row["full_name"],
                "name_ar": row["name_ar"] or row["full_name"],
                "position": label("position", row["position"], lang),
                "patrol": row["patrol"],
                "response": label("response", row["response"], lang),
                "status": label("attendance", row["status"], lang),
                "note": row["note"],
            }
        )
    return {"day": fmt_date(day, lang), "rows": rows, "counts": counts}


def _consent_slips(event: Event, lang: str) -> dict:
    invitations = [i for i in _invitations(event) if i.response != "not_going"]
    links: dict[int, list] = {}
    for link in (
        GuardianLink.objects.filter(member__in=[i.member_id for i in invitations])
        .select_related("guardian")
        .order_by("-is_primary", "pk")
    ):
        links.setdefault(link.member_id, []).append(link)
    tz = event.organization.timezone
    slips = []
    for n, inv in enumerate(invitations, start=1):
        guardians = [
            {
                "full_name": ln.guardian.full_name,
                "relationship": label("relationship", ln.relationship, lang),
                "phone": ln.guardian.phone,
            }
            for ln in links.get(inv.member_id, [])
        ]
        sig = inv.valid_signature
        slips.append(
            {
                "n": n,
                **_member_names(inv.member),
                "guardians": guardians,
                "guardian_names": join([g["full_name"] for g in guardians], lang),
                "consent_text": sig.text if sig else event.consent_text,
                "signed": sig is not None,
                "signed_text": label("yes_no", sig is not None, lang),
                "signed_by": sig.signer_name if sig else "",
                "signed_at": fmt_datetime(sig.signed_at, lang, tz) if sig else "",
            }
        )
    return {"slips": slips}


def custom_values(event: Event, lang: str, overrides: dict | None = None) -> dict[str, dict]:
    """
    {"event": {placeholder: text}, "org": {...}} for the organization's coded fields: the value
    given for this document (by template code), else the stored value, else the default.
    """
    org = event.organization
    out: dict[str, dict] = {"event": {}, "org": {}}
    for field in coded_fields(org):
        code = field.template_code(org.code)
        if overrides and code in overrides:
            value = overrides[code]
        else:
            value, _source = field_value(field, event)
        out[field.spec.code_root][field.placeholder(org.code)] = custom_field_services.display(field, value, lang)
    return out


def build_context(
    kind: str, event: Event, *, lang: str, user, day: date | None = None, overrides: dict | None = None
) -> dict:
    context = _common(event, lang=lang, user=user)
    values = custom_values(event, lang, overrides)
    context["event"].update(values["event"])
    context["org"].update(values["org"])
    if kind == DocumentKind.EVENT_SHEET:
        context.update(_event_sheet(event, lang))
    elif kind == DocumentKind.ATTENDANCE_SHEET:
        context.update(_attendance_sheet(event, lang, day or event.start_date))
    else:
        context.update(_consent_slips(event, lang))
    return context


# ------------------------------------------------------------------ sample data


def sample_context(kind: str, lang: str = "en", organization=None) -> dict:
    """Made-up data in the shape of `build_context`, to test-render an uploaded template."""
    ar = lang == "ar"
    people = [("Jad Haddad", "جاد حداد"), ("Lea Khoury", "ليا خوري"), ("Omar Saleh", "عمر صالح")]
    yes, no = label("yes_no", True, lang), label("yes_no", False, lang)
    context = {
        "org": {"name": "1st Cedar Scouts", "logo": ""},
        "event": {
            "title": "Autumn camp",
            "description": "Two nights in the forest.",
            "type_en": "Activity",
            "type_ar": "نشاط",
            "status": label("status", "published", lang),
            "start_date": fmt_date(date(2030, 10, 23), lang),
            "start_time": "16:00",
            "end_date": fmt_date(date(2030, 10, 25), lang),
            "end_time": "14:00",
            "location": "Cedar forest",
            "cost": "25.00",
            "currency": "USD",
            "reply_deadline": fmt_date(date(2030, 10, 16), lang),
            "is_mandatory": True,
            "is_mandatory_text": yes,
            "requires_consent": True,
            "requires_consent_text": yes,
            "consent_text": "I allow my child to take part in this activity.",
            "units": [{"name_en": "Cubs", "name_ar": "الأشبال"}],
            "unit_names_en": "Cubs",
            "unit_names_ar": "الأشبال",
            "leaders": [{"full_name": "Karim Nassar", "name_ar": "كريم نصار"}],
            "leader_names": "كريم نصار" if ar else "Karim Nassar",
            "leader_names_ar": "كريم نصار",
        },
        "generated_at": fmt_date(date(2030, 10, 1), lang) + " 09:30",
        "generated_by": "Rania Aoun",
    }
    for field in coded_fields(organization):
        sample = custom_field_services.display(field, field.default, lang) or (field.label_ar if ar else field.label_en)
        context[field.spec.code_root][field.placeholder(organization.code)] = sample or field.label_en
    if kind == DocumentKind.EVENT_SHEET:
        context["participants"] = [
            {
                "n": n,
                "full_name": en,
                "name_ar": a,
                "unit": "الأشبال" if ar else "Cubs",
                "response": label("response", "going", lang),
                "waitlisted": False,
                "waitlisted_text": no,
                "signed": True,
                "signed_text": yes,
            }
            for n, (en, a) in enumerate(people, start=1)
        ]
        context["counts"] = {
            "invited": 3,
            "going": 3,
            "maybe": 0,
            "not_going": 0,
            "no_reply": 0,
            "waitlist": 0,
            "signed": 3,
        }
    elif kind == DocumentKind.ATTENDANCE_SHEET:
        context["day"] = fmt_date(date(2030, 10, 23), lang)
        context["counts"] = {"present": 2, "absent": 1, "excused": 0, "unmarked": 0}
        context["rows"] = [
            {
                "n": n,
                "full_name": en,
                "name_ar": a,
                "position": label("position", "member", lang),
                "patrol": "Eagles",
                "response": label("response", "going", lang),
                "status": label("attendance", "present", lang),
                "note": "",
            }
            for n, (en, a) in enumerate(people, start=1)
        ]
    else:
        context["slips"] = [
            {
                "n": n,
                "full_name": en,
                "name_ar": a,
                "guardians": [
                    {
                        "full_name": "Maya Saliba",
                        "relationship": label("relationship", "mother", lang),
                        "phone": "+961 3 000 000",
                    }
                ],
                "guardian_names": "Maya Saliba",
                "consent_text": "I allow my child to take part in this activity.",
                "signed": n == 1,
                "signed_text": yes if n == 1 else no,
                "signed_by": "Maya Saliba" if n == 1 else "",
                "signed_at": (fmt_date(date(2030, 10, 2), lang) + " 18:05") if n == 1 else "",
            }
            for n, (en, a) in enumerate(people, start=1)
        ]
    return context
