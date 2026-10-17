"""
Numbers for the dashboard. The view gathers them per block; each block checks its own
permission, so a block someone can't see comes back as null.

Date filter (`range`): today, week, month, year, all (default) or custom (`from` / `to`).
Only time-based numbers follow it: attendance, and members joined / left.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.attendance.models import AttendanceRecord, AttendanceStatus
from apps.attendance.views import scoped_sessions, visible_unit_ids
from apps.events.models import Event, EventStatus
from apps.events.services import visible_events, with_counts
from apps.inventory import services as inventory
from apps.inventory.models import NEEDS_CARE, Item, ItemKind, Reservation
from apps.members.models import Gender, Member, MemberKind, MemberStatus
from apps.units.models import Unit, UnitMembership

RANGES = ("today", "week", "month", "year", "all", "custom")
MAX_CUSTOM_DAYS = 5 * 366
UPCOMING_DAYS = 21
CONSENT_DAYS = 30
REPLY_SOON_DAYS = 7
MISSING_WINDOW = 6  # "missing lately": the last 6 mandatory meetings
MISSING_MIN_ABSENT = 3


# ---------------------------------------------------------------- the period


@dataclass
class Period:
    key: str
    start: date | None  # None until resolved for "all" (then the first day with data)
    end: date
    previous: tuple[date, date] | None = None
    bucket: str = "day"

    def as_dict(self):
        return {
            "range": self.key,
            "start": self.start,
            "end": self.end,
            "bucket": self.bucket,
            "previous": {"start": self.previous[0], "end": self.previous[1]} if self.previous else None,
        }


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError({field: [_("Enter a valid date (YYYY-MM-DD).")]}) from None


def bucket_for(start: date, end: date) -> str:
    """The chart groups by day up to 2 weeks, by week up to 3 months, by month up to 2 years."""
    days = (end - start).days + 1
    if days <= 14:
        return "day"
    if days <= 92:
        return "week"
    if days <= 731:
        return "month"
    return "year"


def resolve_period(params, today: date) -> Period:
    key = params.get("range") or "all"
    if key not in RANGES:
        raise ValidationError({"range": [_("Unknown range.")]})
    if key == "today":
        return Period(key, today, today, (today - timedelta(days=1),) * 2, "day")
    if key == "week":
        start = today - timedelta(days=today.weekday())  # weeks start on Monday
        return Period(key, start, start + timedelta(days=6), (start - timedelta(days=7), start - timedelta(days=1)))
    if key == "month":
        start = today.replace(day=1)
        prev_end = start - timedelta(days=1)
        return Period(key, start, _month_end(today), (prev_end.replace(day=1), prev_end), "week")
    if key == "year":
        start = date(today.year, 1, 1)
        return Period(
            key, start, date(today.year, 12, 31), (date(today.year - 1, 1, 1), start - timedelta(days=1)), "month"
        )
    if key == "custom":
        start = _parse_date(params.get("from"), "from")
        end = _parse_date(params.get("to"), "to")
        if end < start:
            raise ValidationError({"to": [_("The end date must be on or after the start date.")]})
        if (end - start).days + 1 > MAX_CUSTOM_DAYS:
            raise ValidationError({"to": [_("Pick a range of 5 years or less.")]})
        length = end - start
        prev_end = start - timedelta(days=1)
        return Period(key, start, end, (prev_end - length, prev_end), bucket_for(start, end))
    return Period(key, None, today, None)  # all time: the start is set from the data


def buckets(period: Period) -> list[tuple[date, date]]:
    """Consecutive slices of the period: days, weeks (from Monday), months or years."""
    out = []
    cursor = period.start
    while cursor <= period.end:
        if period.bucket == "day":
            stop = cursor
        elif period.bucket == "week":
            stop = cursor + timedelta(days=6 - cursor.weekday())
        elif period.bucket == "month":
            stop = _month_end(cursor)
        else:
            stop = date(cursor.year, 12, 31)
        stop = min(stop, period.end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


# ---------------------------------------------------------------- attendance


def _session_units(session) -> set[int] | None:
    """Units a session belongs to; None for an organization-wide event (it counts for every unit)."""
    if session.unit_id:
        return {session.unit_id}
    ids = {u.pk for u in session.event.units.all()} if session.event_id else set()
    return ids or None


def _tally(sessions, member_ids: set[int] | None, unit: int | None, unit_members: dict[int, set[int]]):
    """Present / counted records per session, for everyone in scope or for one unit."""
    rows = []
    for s in sessions:
        if unit is not None:
            units = _session_units(s)
            if units is not None and unit not in units:
                continue
            allowed = unit_members.get(unit, set())
        else:
            allowed = member_ids
        present = counted = 0
        for r in s.records.all():
            if r.status == AttendanceStatus.EXCUSED:
                continue
            if (s.event_id or unit is not None) and allowed is not None and r.member_id not in allowed:
                continue
            counted += 1
            present += r.status == AttendanceStatus.PRESENT
        if counted:
            rows.append((s.date, present, counted))
    return rows


def _rate(rows, start, end):
    present = sum(p for d, p, c in rows if start <= d <= end)
    counted = sum(c for d, p, c in rows if start <= d <= end)
    return round(100 * present / counted) if counted else None


def attendance_block(service, org, period: Period, today: date):
    """Rate over the period (pooled), per chart slice, per unit, and the previous period's rate."""
    visible = visible_unit_ids(service)
    if visible is not None and not visible:
        return None
    base = scoped_sessions(org, visible).filter(date__lte=today)
    if period.start is None:
        first = base.order_by("date").values_list("date", flat=True).first()
        period.start = first or today
        period.bucket = bucket_for(period.start, period.end)
    low = period.previous[0] if period.previous else period.start
    sessions = list(
        base.filter(date__gte=low, date__lte=period.end)
        .select_related("event")
        .prefetch_related("records", "event__units")
    )
    unit_qs = Unit.objects.filter(organization=org, is_active=True)
    if visible is not None:
        unit_qs = unit_qs.filter(pk__in=visible)
    unit_ids = list(unit_qs.values_list("pk", flat=True))
    unit_members: dict[int, set[int]] = defaultdict(set)
    for unit_id, member_id in UnitMembership.objects.filter(unit_id__in=unit_ids).values_list("unit_id", "member_id"):
        unit_members[unit_id].add(member_id)
    in_scope = None if visible is None else set().union(*unit_members.values()) if unit_members else set()

    slices = buckets(period)

    def series(unit):
        rows = _tally(sessions, in_scope, unit, unit_members)
        values = []
        for start, end in slices:
            values.append(None if start > today else _rate(rows, start, end))
        return {
            "average": _rate(rows, period.start, period.end),
            "previous": _rate(rows, *period.previous) if period.previous else None,
            "values": values,
        }

    return {
        "buckets": [{"start": s, "end": e, "future": s > today} for s, e in slices],
        "all": series(None),
        "units": {str(u): series(u) for u in unit_ids},
        "missing_lately": missing_lately(org, visible, in_scope, today),
    }


def missing_lately(org, visible, in_scope, today):
    """Active youth who missed 3 or more of their last 6 mandatory meetings (excused doesn't count)."""
    records = (
        AttendanceRecord.objects.filter(
            session__organization=org,
            session__is_mandatory=True,
            session__date__lte=today,
            session__date__gte=today - timedelta(days=365),
            member__status=MemberStatus.ACTIVE,
            member__kind=MemberKind.YOUTH,
        )
        .exclude(status=AttendanceStatus.EXCUSED)
        .order_by("member_id", "-session__date", "-session_id")
        .values_list("member_id", "status", "session__date")
    )
    if in_scope is not None:
        records = records.filter(member_id__in=in_scope)
    recent: dict[int, list] = defaultdict(list)
    for member_id, status, day in records:
        if len(recent[member_id]) < MISSING_WINDOW:
            recent[member_id].append((status, day))
    flagged = {
        m: rows
        for m, rows in recent.items()
        if sum(s == AttendanceStatus.ABSENT for s, _d in rows) >= MISSING_MIN_ABSENT
    }
    if not flagged:
        return []
    seen: dict[int, date] = {}
    for member_id, day in (
        AttendanceRecord.objects.filter(member_id__in=flagged, status=AttendanceStatus.PRESENT)
        .order_by("-session__date")
        .values_list("member_id", "session__date")
    ):
        seen.setdefault(member_id, day)
    members = Member.objects.filter(pk__in=flagged).prefetch_related("unit_memberships")
    out = []
    for m in members:
        rows = flagged[m.pk]
        out.append(
            {
                "member_id": m.pk,
                "full_name": m.full_name,
                "name_ar": m.name_ar,
                "units": [um.unit_id for um in m.unit_memberships.all()],
                "last_seen": seen.get(m.pk),
                "absent": sum(s == AttendanceStatus.ABSENT for s, _d in rows),
                # oldest first, for the little present/absent strip
                "recent": [s for s, _d in reversed(rows)],
            }
        )
    out.sort(key=lambda r: (-r["absent"], r["last_seen"] or date.min))
    return out[:8]


# ---------------------------------------------------------------- members


def _age_on(dob: date, day: date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def membership_block(org, visible_people, period: Period, today: date):
    """Joined / left in the period (org-wide counts), age and gender mix, birthdays this week."""
    everyone = Member.objects.filter(organization=org)
    active = everyone.filter(status=MemberStatus.ACTIVE)
    if period.key == "all":
        joined = everyone.count()
        gone = everyone.exclude(status=MemberStatus.ACTIVE)
    else:
        start = period.start or today
        joined = everyone.filter(join_date__gte=start, join_date__lte=period.end).count()
        gone = everyone.filter(left_date__gte=start, left_date__lte=period.end).exclude(status=MemberStatus.ACTIVE)
    left_inactive = gone.filter(status=MemberStatus.INACTIVE).count()
    left_alumni = gone.filter(status=MemberStatus.ALUMNI).count()

    ages: dict[int, int] = defaultdict(int)
    for dob in active.filter(kind=MemberKind.YOUTH, date_of_birth__isnull=False).values_list(
        "date_of_birth", flat=True
    ):
        ages[_age_on(dob, today)] += 1
    genders = {g: 0 for g in (Gender.MALE, Gender.FEMALE)}
    unknown = 0
    for g in active.values_list("gender", flat=True):
        if g in genders:
            genders[g] += 1
        else:
            unknown += 1

    week_start = today - timedelta(days=today.weekday())
    week = [week_start + timedelta(days=i) for i in range(7)]
    birthdays = []
    for m in visible_people.filter(date_of_birth__isnull=False).only(
        "id", "first_name", "last_name", "first_name_ar", "last_name_ar", "date_of_birth"
    ):
        dob = m.date_of_birth
        for day in week:
            if (dob.month, dob.day) == (day.month, day.day) or (
                (dob.month, dob.day) == (2, 29) and (day.month, day.day) == (2, 28) and day.year % 4
            ):
                birthdays.append(
                    {
                        "member_id": m.pk,
                        "full_name": m.full_name,
                        "name_ar": m.name_ar,
                        "date": day,
                        "turns": _age_on(dob, day),
                    }
                )
    birthdays.sort(key=lambda b: b["date"])

    return {
        "active": active.count(),
        "joined": joined,
        "left": left_inactive + left_alumni,
        "left_inactive": left_inactive,
        "left_alumni": left_alumni,
        "ages": [{"age": a, "count": n} for a, n in sorted(ages.items()) if 0 <= a < 30],
        "youth": active.filter(kind=MemberKind.YOUTH).count(),
        "gender": {"male": genders[Gender.MALE], "female": genders[Gender.FEMALE], "unknown": unknown},
        "birthdays": birthdays[:8],
    }


def move_up(visible_people, today: date):
    """Active youth older than the age range of one of their units."""
    rows = UnitMembership.objects.filter(
        member__in=visible_people.filter(kind=MemberKind.YOUTH, date_of_birth__isnull=False),
        unit__age_max__isnull=False,
        unit__is_active=True,
    ).values_list("member_id", "member__date_of_birth", "unit__age_max")
    return len({m for m, dob, age_max in rows if _age_on(dob, today) > age_max})


# ---------------------------------------------------------------- events


def _event_brief(e: Event) -> dict:
    return {
        "id": e.pk,
        "title": e.title,
        "start_date": e.start_date,
        "end_date": e.end_date,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "type": {
            "id": e.event_type_id,
            "name_en": e.event_type.name_en,
            "name_ar": e.event_type.name_ar,
            "color": e.event_type.color,
            "icon": e.event_type.icon,
        },
        "units": [u.pk for u in e.units.all()],
        "is_mandatory": e.is_mandatory,
        "requires_consent": e.requires_consent,
        "reply_deadline": e.reply_deadline,
        "capacity": e.capacity,
        "invited": e.n_invited,
        "going": e.n_going,
        "maybe": e.n_maybe,
        "not_going": e.n_not_going,
        "no_reply": e.n_no_reply,
        "waitlist": e.n_waitlist,
        "signed": e.n_signed,
    }


def events_block(service, org, today: date):
    """Upcoming published events, and the event-based items for Needs attention."""
    visible = visible_events(service, org)
    upcoming = list(
        with_counts(
            Event.objects.filter(
                pk__in=visible.values("pk"),
                status=EventStatus.PUBLISHED,
                end_date__gte=today,
                start_date__lte=today + timedelta(days=CONSENT_DAYS),
            )
        )
        .select_related("event_type")
        .prefetch_related("units")
        .order_by("start_date", "start_time", "id")
    )
    soon = today + timedelta(days=REPLY_SOON_DAYS)
    consent, replies, waitlist = [], [], []
    for e in upcoming:
        brief = {"event_id": e.pk, "title": e.title, "start_date": e.start_date}
        if e.requires_consent and e.n_going > e.n_signed and not e.is_closed:
            consent.append({**brief, "count": e.n_going - e.n_signed})
        due = e.reply_deadline
        if e.replies_open and e.n_no_reply and ((due and due <= soon) or e.start_date <= soon):
            replies.append({**brief, "count": e.n_no_reply, "reply_deadline": due})
        if e.n_waitlist:
            waitlist.append({**brief, "count": e.n_waitlist, "capacity": e.capacity})
    return {
        "upcoming": [_event_brief(e) for e in upcoming if e.start_date <= today + timedelta(days=UPCOMING_DAYS)][:6],
        "consent": consent,
        "replies": replies,
        "waitlist": waitlist,
    }


# ---------------------------------------------------------------- equipment


def equipment_block(org, today: date):
    items = list(Item.objects.filter(organization=org, is_archived=False).prefetch_related("pieces"))
    out_now = 0
    open_lines = list(
        inventory.open_lines(org).values("item_id", "piece_id", "quantity", "returned_quantity", "lost_quantity")
    )
    kinds = {i.pk: i.kind for i in items}
    for line in open_lines:
        if kinds.get(line["item_id"]) == ItemKind.CONSUMABLE:
            continue
        out_now += 1 if line["piece_id"] else line["quantity"] - line["returned_quantity"] - line["lost_quantity"]
    needs_care = 0
    low = []
    for item in items:
        if item.kind == ItemKind.TRACKED:
            needs_care += sum(1 for p in item.pieces.all() if p.condition in NEEDS_CARE)
        elif item.kind == ItemKind.COUNTED:
            needs_care += item.damaged_quantity
        elif item.min_stock is not None and item.quantity < item.min_stock:
            low.append({"item": item.pk, "name_en": item.name_en, "name_ar": item.name_ar,
                        "quantity": item.quantity, "min_stock": item.min_stock})  # fmt: skip
    reserved = Reservation.objects.filter(
        organization=org,
        loan_line__isnull=True,
        event__end_date__gte=today,
        event__start_date__lte=today + timedelta(days=30),
    ).count()
    overdue = list(
        inventory.overdue_loans(org)
        .select_related("event", "unit", "member", "external")
        .prefetch_related("lines__item", "lines__piece")
        .order_by("due_date", "id")
    )
    loans = []
    for loan in overdue[:5]:
        lines = [ln for ln in loan.lines.all() if ln.outstanding > 0]
        loans.append(
            {
                "id": loan.pk,
                "borrower_type": loan.borrower_type,
                "borrower": loan.borrower_name,
                "due_date": loan.due_date,
                "days_late": (today - loan.due_date).days,
                "lines": [
                    {
                        "name_en": ln.item.name_en,
                        "name_ar": ln.item.name_ar,
                        "code": ln.piece.code if ln.piece_id else "",
                        "quantity": ln.outstanding,
                    }
                    for ln in lines
                ],  # fmt: skip
            }
        )
    return {
        "out_now": out_now,
        "overdue": len(overdue),
        "needs_care": needs_care,
        "reserved": reserved,
        "low_stock": low,
        "overdue_loans": loans,
    }
