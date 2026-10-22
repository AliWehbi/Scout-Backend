"""
Members export to Excel: the user picks who (leaders, members or both), which units and
statuses, and which columns in which order.

"Leader" means a leader or assistant leader position in one of the exported units (any unit
when none are picked); everyone else is a member. Leaders come first.

Medical columns are only offered with "View medical info", and filled only for members the
user may see medical info for (the permission can be granted per unit).
"""

from dataclasses import dataclass
from datetime import date
from io import BytesIO

from django.utils.translation import gettext as _
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.units.models import Unit

from . import services
from .models import CustomField, FieldSection, FieldType, Member, MemberStatus

GROUPS = ("leaders", "members")
MAX_FIELDS = 80

# Labels for stored values, in both languages (the backend has no Arabic message catalog).
VALUE_LABELS = {
    "gender": {"male": ("Male", "ذكر"), "female": ("Female", "أنثى")},
    "kind": {"youth": ("Youth member", "عضو شاب"), "adult": ("Adult", "بالغ")},
    "status": {"active": ("Active", "نشط"), "inactive": ("Inactive", "غير نشط"), "alumni": ("Alumni", "قدامى")},
    "position": {
        "leader": ("Leader", "قائد"),
        "assistant": ("Assistant leader", "قائد مساعد"),
        "patrol_leader": ("Patrol leader", "رئيس طليعة"),
        "member": ("Member", "عضو"),
    },
    "relationship": {
        "mother": ("Mother", "الأم"),
        "father": ("Father", "الأب"),
        "guardian": ("Guardian", "ولي الأمر"),
        "grandparent": ("Grandparent", "الجد/الجدة"),
        "sibling": ("Sibling", "الأخ/الأخت"),
        "other": ("Other", "آخر"),
    },
    "severity": {"mild": ("mild", "خفيفة"), "moderate": ("moderate", "متوسطة"), "severe": ("severe", "شديدة")},
    "group": {"leaders": ("Leader", "قائد"), "members": ("Member", "عضو")},
    "yes_no": {True: ("Yes", "نعم"), False: ("No", "لا")},
}


def label(group: str, value, lang: str) -> str:
    pair = VALUE_LABELS[group].get(value)
    if pair is None:
        return "" if value in (None, "") else str(value)
    return pair[1] if lang == "ar" else pair[0]


@dataclass
class Row:
    member: Member
    group: str
    memberships: list  # unit memberships within the exported units
    medical_ok: bool
    lang: str

    @property
    def medical(self):
        return getattr(self.member, "medical", None) if self.medical_ok else None


def _join(values, lang):
    return ("، " if lang == "ar" else ", ").join(str(v) for v in values if v)


def _age(member: Member):
    born = member.date_of_birth
    if not born:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _unit_name(unit, lang):
    return (unit.name_ar or unit.name_en) if lang == "ar" else unit.name_en


def _guardians(row: Row, attr: str):
    links = sorted(row.member.guardian_links.all(), key=lambda ln: (not ln.is_primary, ln.pk))
    return [getattr(ln.guardian, attr) for ln in links]


def _medical_list(row: Row, attr: str, fmt):
    medical = row.medical
    return _join([fmt(item) for item in (getattr(medical, attr, None) or [])], row.lang) if medical else ""


def _medical_value(attr: str):
    return lambda row: getattr(row.medical, attr, "") if row.medical else ""


# key -> (English, Arabic, section, value function). Order here is the order in the picker.
BUILT_IN = {
    "group": ("Leader / member", "قائد / عضو", "personal", lambda r: label("group", r.group, r.lang)),
    "full_name": ("Full name", "الاسم الكامل", "personal", lambda r: r.member.full_name),
    "full_name_ar": ("Full name (Arabic)", "الاسم الكامل (عربي)", "personal", lambda r: r.member.name_ar),
    "first_name": ("First name", "الاسم الأول", "personal", lambda r: r.member.first_name),
    "last_name": ("Last name", "اسم العائلة", "personal", lambda r: r.member.last_name),
    "first_name_ar": ("First name (Arabic)", "الاسم الأول (عربي)", "personal", lambda r: r.member.first_name_ar),
    "last_name_ar": ("Last name (Arabic)", "اسم العائلة (عربي)", "personal", lambda r: r.member.last_name_ar),
    "date_of_birth": ("Date of birth", "تاريخ الميلاد", "personal", lambda r: r.member.date_of_birth),
    "age": ("Age", "العمر", "personal", lambda r: _age(r.member)),
    "gender": ("Gender", "الجنس", "personal", lambda r: label("gender", r.member.gender, r.lang)),
    "kind": ("Type", "النوع", "personal", lambda r: label("kind", r.member.kind, r.lang)),
    "status": ("Status", "الحالة", "personal", lambda r: label("status", r.member.status, r.lang)),
    "join_date": ("Joined on", "تاريخ الانضمام", "personal", lambda r: r.member.join_date),
    "has_login": (
        "Has a login",
        "لديه حساب",
        "personal",
        lambda r: label("yes_no", r.member.membership_id is not None, r.lang),
    ),
    "notes": ("Notes", "ملاحظات", "personal", lambda r: r.member.notes),
    "phone": ("Phone", "الهاتف", "contact", lambda r: r.member.phone),
    "email": ("Email", "البريد الإلكتروني", "contact", lambda r: r.member.email),
    "address": ("Address", "العنوان", "contact", lambda r: r.member.address),
    "units": (
        "Unit",
        "الوحدة",
        "units",
        lambda r: _join([_unit_name(um.unit, r.lang) for um in r.memberships], r.lang),
    ),
    "position": (
        "Position",
        "المنصب",
        "units",
        lambda r: _join(dict.fromkeys(label("position", um.position, r.lang) for um in r.memberships), r.lang),
    ),
    "patrol": (
        "Patrol",
        "الطليعة",
        "units",
        lambda r: _join(
            [
                (um.patrol.name_ar or um.patrol.name) if r.lang == "ar" else um.patrol.name
                for um in r.memberships
                if um.patrol
            ],
            r.lang,
        ),
    ),
    "guardian_names": (
        "Parents / guardians",
        "أولياء الأمور",
        "guardians",
        lambda r: _join(
            [
                f"{ln.guardian.full_name} ({label('relationship', ln.relationship, r.lang)})"
                for ln in sorted(r.member.guardian_links.all(), key=lambda ln: (not ln.is_primary, ln.pk))
            ],
            r.lang,
        ),
    ),
    "guardian_phones": (
        "Guardian phones",
        "هواتف أولياء الأمور",
        "guardians",
        lambda r: _join(_guardians(r, "phone"), r.lang),
    ),
    "guardian_emails": (
        "Guardian emails",
        "بريد أولياء الأمور",
        "guardians",
        lambda r: _join(_guardians(r, "email"), r.lang),
    ),
    "allergies": (
        "Allergies",
        "الحساسية",
        "medical",
        lambda r: _medical_list(
            r, "allergies", lambda a: f"{a.get('name', '')} ({label('severity', a.get('severity'), r.lang)})"
        ),
    ),
    "medications": (
        "Medications",
        "الأدوية",
        "medical",
        lambda r: _medical_list(
            r, "medications", lambda m: " ".join(x for x in (m.get("name"), m.get("dose"), m.get("when")) if x)
        ),
    ),
    "conditions": ("Medical conditions", "الحالات الطبية", "medical", _medical_value("conditions")),
    "blood_type": ("Blood type", "فصيلة الدم", "medical", _medical_value("blood_type")),
    "emergency_contacts": (
        "Emergency contacts",
        "جهات الاتصال للطوارئ",
        "medical",
        lambda r: _medical_list(
            r,
            "emergency_contacts",
            lambda c: " ".join(
                x
                for x in (c.get("name"), f"({c['relationship']})" if c.get("relationship") else "", c.get("phone"))
                if x
            ),
        ),
    ),
    "doctor_name": ("Doctor", "الطبيب", "medical", _medical_value("doctor_name")),
    "doctor_phone": ("Doctor's phone", "هاتف الطبيب", "medical", _medical_value("doctor_phone")),
    "insurance": ("Insurance", "التأمين", "medical", _medical_value("insurance")),
    "medical_notes": ("Medical notes", "ملاحظات طبية", "medical", _medical_value("notes")),
}

DEFAULT_FIELDS = [
    "group",
    "full_name",
    "full_name_ar",
    "units",
    "position",
    "date_of_birth",
    "phone",
    "guardian_names",
    "guardian_phones",
]


def _custom_value(field: CustomField):
    def value(row: Row):
        if field.section == FieldSection.MEDICAL and not row.medical_ok:
            return ""
        raw = (row.member.custom_data or {}).get(field.key)
        if raw in (None, "", []):
            return ""
        if field.field_type == FieldType.YES_NO:
            return label("yes_no", bool(raw), row.lang)
        if field.field_type == FieldType.MULTI and isinstance(raw, list):
            return _join(raw, row.lang)
        if field.field_type == FieldType.DATE:
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                return str(raw)
        return raw

    return value


def catalog(organization, service) -> dict:
    """Every field this user may export: key -> (English, Arabic, section, value function)."""
    medical = service.has_perm_anywhere("members.view_medical")
    fields = {k: v for k, v in BUILT_IN.items() if medical or v[2] != "medical"}
    for f in CustomField.objects.for_entity(organization, "member").filter(is_active=True).order_by("order", "id"):
        if f.section == FieldSection.MEDICAL and not medical:
            continue
        section = "medical" if f.section == FieldSection.MEDICAL else "custom"
        fields[f"custom__{f.key}"] = (f.label_en, f.label_ar or f.label_en, section, _custom_value(f))
    return fields


def catalog_payload(organization, service) -> dict:
    fields = catalog(organization, service)
    return {
        "fields": [{"key": k, "en": en, "ar": ar, "section": section} for k, (en, ar, section, _fn) in fields.items()],
        "default": [k for k in DEFAULT_FIELDS if k in fields],
    }


# ------------------------------------------------------------------ selecting people


def _csv_ints(value: str, name: str) -> list[int]:
    try:
        return [int(v) for v in value.split(",") if v.strip()] if value else []
    except ValueError:
        raise ValidationError({name: [_("Invalid value.")]}) from None


def _csv_choice(value: str, name: str, choices, default) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()] if value else list(default)
    bad = [v for v in items if v not in choices]
    if bad or not items:
        raise ValidationError({name: [_("Invalid value.")]})
    return list(dict.fromkeys(items))


@dataclass
class ExportRequest:
    fields: list[str]
    unit_ids: list[int]
    groups: list[str]
    statuses: list[str]
    lang: str


def parse_request(params, organization, service, lang: str) -> ExportRequest:
    available = catalog(organization, service)
    fields = list(dict.fromkeys(f for f in (params.get("fields") or "").split(",") if f))
    if not fields:
        raise ValidationError({"fields": [_("Choose at least one field.")]})
    unknown = [f for f in fields if f not in available]
    if unknown or len(fields) > MAX_FIELDS:
        raise ValidationError({"fields": [_("Unknown or unavailable field: %(f)s") % {"f": ", ".join(unknown)}]})

    unit_ids = _csv_ints(params.get("units", ""), "units")
    if unit_ids:
        found = set(Unit.objects.filter(organization=organization, pk__in=unit_ids).values_list("pk", flat=True))
        if found != set(unit_ids):
            raise ValidationError({"units": [_("Unknown unit.")]})
        scope = service.unit_ids("members.view")
        if scope is not None and not set(unit_ids) <= scope:
            raise PermissionDenied(_("You can only export members of your own units."))

    return ExportRequest(
        fields=fields,
        unit_ids=unit_ids,
        groups=_csv_choice(params.get("groups", ""), "groups", GROUPS, GROUPS),
        statuses=_csv_choice(params.get("statuses", ""), "statuses", MemberStatus.values, [MemberStatus.ACTIVE]),
        lang=lang,
    )


def build_rows(qs, req: ExportRequest, service) -> list[Row]:
    """`qs` is already limited to the members this user may view."""
    qs = qs.filter(status__in=req.statuses)
    if req.unit_ids:
        qs = qs.filter(unit_memberships__unit_id__in=req.unit_ids)
    wanted = set(req.unit_ids)
    medical_scope = service.unit_ids("members.view_medical")
    medical_everywhere = medical_scope is None and service.has_perm("members.view_medical")

    rows = []
    for member in qs.distinct().order_by("first_name", "last_name", "pk"):
        memberships = sorted(
            (um for um in member.unit_memberships.all() if not wanted or um.unit_id in wanted),
            key=lambda um: (um.unit.name_en.lower(), um.pk),
        )
        is_leader = any(um.position in services.LEADER_POSITIONS for um in memberships)
        group = "leaders" if is_leader else "members"
        if group not in req.groups:
            continue
        member_units = {um.unit_id for um in member.unit_memberships.all()}
        medical_ok = medical_everywhere or bool(medical_scope and medical_scope & member_units)
        rows.append(Row(member, group, memberships, medical_ok, req.lang))
    rows.sort(key=lambda r: GROUPS.index(r.group))  # stable: keeps name order inside each group
    return rows


# ------------------------------------------------------------------ the workbook


def _safe(value):
    """Plain values for a cell. Text is never a formula (see `write_workbook`)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return value


def write_workbook(rows: list[Row], req: ExportRequest, organization, service) -> bytes:
    available = catalog(organization, service)
    columns = [(key, available[key]) for key in req.fields]
    ar = req.lang == "ar"

    wb = Workbook()
    ws = wb.active
    ws.title = "الأعضاء" if ar else "Members"
    ws.sheet_view.rightToLeft = ar
    header_fill = PatternFill("solid", fgColor="E8EEF4")
    widths = []
    for c, (_key, (en, label_ar, _section, _fn)) in enumerate(columns, start=1):
        title = label_ar if ar else en
        cell = ws.cell(row=1, column=c, value=title)
        cell.data_type = "s"
        cell.font = Font(bold=True)
        cell.fill = header_fill
        widths.append(len(title))

    for r, row in enumerate(rows, start=2):
        for c, (_key, (_en, _ar, _section, fn)) in enumerate(columns, start=1):
            value = _safe(fn(row))
            cell = ws.cell(row=r, column=c, value=value)
            if isinstance(value, str):
                # openpyxl stores text starting with "=" as a formula: a member named
                # "=HYPERLINK(...)" must stay text (formula injection).
                cell.data_type = "s"
                if value[:1] in ("=", "+", "-", "@"):
                    cell.quotePrefix = True
                width = max((len(line) for line in value.splitlines()), default=0)
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
                width = 10
            else:
                width = len(str(value))
            widths[c - 1] = max(widths[c - 1], width)

    for c, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 8), 50)
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = Alignment(vertical="top")
    ws.freeze_panes = "A2"
    if columns:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 1)}"

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
