from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.attendance.models import AttendanceRecord, Session
from apps.events.models import Event, EventInvitation, EventType
from apps.events.services import create_default_event_types
from apps.inventory.models import Item, Loan, LoanLine
from apps.members.models import Member
from apps.organizations.dashboard import buckets, resolve_period
from apps.units.models import UnitMembership

THU = date(2026, 10, 15)


@pytest.mark.parametrize(
    "params, start, end, bucket, previous",
    [
        ({"range": "today"}, THU, THU, "day", (date(2026, 10, 14), date(2026, 10, 14))),
        ({"range": "week"}, date(2026, 10, 12), date(2026, 10, 18), "day", (date(2026, 10, 5), date(2026, 10, 11))),
        ({"range": "month"}, date(2026, 10, 1), date(2026, 10, 31), "week", (date(2026, 9, 1), date(2026, 9, 30))),
        ({"range": "year"}, date(2026, 1, 1), date(2026, 12, 31), "month", (date(2025, 1, 1), date(2025, 12, 31))),
        (
            {"range": "custom", "from": "2026-09-21", "to": "2026-10-15"},
            date(2026, 9, 21),
            date(2026, 10, 15),
            "week",
            (date(2026, 8, 27), date(2026, 9, 20)),
        ),
    ],
)
def test_periods(params, start, end, bucket, previous):
    p = resolve_period(params, THU)
    assert (p.start, p.end, p.bucket, p.previous) == (start, end, bucket, previous)


def test_all_time_is_default_and_buckets_split_on_mondays():
    p = resolve_period({}, THU)
    assert (p.key, p.start, p.end, p.previous) == ("all", None, THU, None)
    month = resolve_period({"range": "month"}, THU)
    assert [s.day for s, _e in buckets(month)] == [1, 5, 12, 19, 26]
    assert buckets(month)[0] == (date(2026, 10, 1), date(2026, 10, 4))


@pytest.mark.parametrize(
    "params",
    [
        {"range": "decade"},
        {"range": "custom", "from": "2026-10-10"},
        {"range": "custom", "from": "2026-10-10", "to": "2026-10-01"},
        {"range": "custom", "from": "2010-01-01", "to": "2026-01-01"},
    ],
)
def test_bad_ranges(params):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        resolve_period(params, THU)


def test_left_date_follows_status(world):
    org = world.org()
    today = date.today()
    m = Member.objects.create(organization=org, first_name="Jad")
    assert m.join_date == today and m.left_date is None
    m.status = "inactive"
    m.save()
    assert m.left_date == today
    m.left_date = today - timedelta(days=30)
    m.save()
    m.status = "alumni"  # inactive -> alumni keeps the date they left
    m.save(update_fields=["status"])
    m.refresh_from_db()
    assert m.left_date == today - timedelta(days=30)
    m.status = "active"
    m.save(update_fields=["status"])
    m.refresh_from_db()
    assert m.left_date is None
    # created as alumni (e.g. imported): no left date
    assert Member.objects.create(organization=org, first_name="Old", status="alumni").left_date is None


def test_bulk_status_sets_left_date(world, client_for):
    org = world.org()
    admin = world.member(org, world.user("rania"), owner=True)
    m = Member.objects.create(organization=org, first_name="Jad")
    res = client_for(admin.user, org).post(
        "/api/v1/members/bulk/", {"ids": [m.pk], "action": "set_status", "status": "alumni"}, format="json"
    )
    assert res.status_code in (200, 204), res.data
    m.refresh_from_db()
    assert (m.status, m.left_date) == ("alumni", date.today())


@pytest.fixture
def group(world):
    org = world.org()
    create_default_event_types(org)
    cubs, scouts = world.unit(org, "Cubs"), world.unit(org, "Scouts")
    cubs.age_max = 10
    cubs.save()
    admin = world.member(org, world.user("rania"), owner=True)
    karim = world.member(org, world.user("karim"), roles=[("unit_leader", cubs)])
    today = timezone.localdate()

    def kid(name, unit, **extra):
        m = Member.objects.create(organization=org, first_name=name, **extra)
        UnitMembership.objects.create(member=m, unit=unit)
        return m

    jad = kid("Jad", cubs, date_of_birth=today.replace(year=today.year - 12), gender="male")  # too old for Cubs
    lea = kid("Lea", cubs, gender="female")
    omar = kid("Omar", scouts)
    return dict(org=org, cubs=cubs, scouts=scouts, admin=admin, karim=karim, jad=jad, lea=lea, omar=omar, today=today)


def take(org, unit, day, marks, mandatory=True):
    s = Session.objects.create(organization=org, unit=unit, title="Meeting", date=day, is_mandatory=mandatory)
    for member, status in marks:
        AttendanceRecord.objects.create(session=s, member=member, status=status)
    return s


def summary(client, **params):
    res = client.get("/api/v1/dashboard/summary/", params)
    assert res.status_code == 200, res.data
    return res.data


def test_attendance_by_range_and_unit(group, client_for):
    g, today = group, group["today"]
    start = today - timedelta(days=20)
    take(g["org"], g["cubs"], start, [(g["jad"], "present"), (g["lea"], "absent")])  # 50%
    take(g["org"], g["cubs"], start + timedelta(days=7), [(g["jad"], "present"), (g["lea"], "present")])  # 100%
    take(g["org"], g["scouts"], start + timedelta(days=7), [(g["omar"], "absent")])  # 0%
    take(g["org"], g["cubs"], start - timedelta(days=5), [(g["jad"], "absent"), (g["lea"], "excused")])  # previous
    admin = client_for(g["admin"].user, g["org"])

    data = summary(admin, range="custom", **{"from": start.isoformat(), "to": today.isoformat()})
    att = data["attendance"]
    assert data["period"]["range"] == "custom" and data["period"]["bucket"] == "week"
    assert att["all"]["average"] == 60  # 3 present of 5 counted
    assert att["all"]["previous"] == 0  # excused is left out
    assert att["units"][str(g["cubs"].pk)]["average"] == 75
    assert att["units"][str(g["scouts"].pk)]["average"] == 0
    assert len(att["buckets"]) == len(att["all"]["values"])
    assert [v for v in att["all"]["values"] if v is not None] == [50, 67]

    # All time (the default) starts at the first meeting and has nothing to compare with.
    data = summary(admin)
    assert data["period"]["range"] == "all"
    assert data["period"]["start"] == start - timedelta(days=5)
    assert data["attendance"]["all"]["previous"] is None

    # A Cubs leader only sees Cubs.
    karim = summary(client_for(g["karim"].user, g["org"]))
    assert list(karim["attendance"]["units"]) == [str(g["cubs"].pk)]


def test_missing_lately(group, client_for):
    g, today = group, group["today"]
    for i, status in enumerate(["present", "absent", "absent", "present", "absent", "excused"]):
        take(g["org"], g["cubs"], today - timedelta(days=40 - i * 7), [(g["lea"], status), (g["jad"], "present")])
    take(g["org"], g["cubs"], today - timedelta(days=2), [(g["lea"], "absent")], mandatory=False)  # not counted
    rows = summary(client_for(g["admin"].user, g["org"]))["attendance"]["missing_lately"]
    assert [r["full_name"] for r in rows] == ["Lea"]
    assert rows[0]["recent"] == ["present", "absent", "absent", "present", "absent"]
    assert rows[0]["last_seen"] == today - timedelta(days=40 - 3 * 7)


def test_membership_numbers(group, client_for):
    g = group
    g["omar"].status = "alumni"
    g["omar"].save()
    Member.objects.create(organization=g["org"], first_name="Old", join_date=date(2020, 1, 1))
    data = summary(client_for(g["admin"].user, g["org"]), range="week")
    mem = data["membership"]
    assert (mem["joined"], mem["left"], mem["left_alumni"], mem["active"]) == (3, 1, 1, 3)
    assert mem["gender"] == {"male": 1, "female": 1, "unknown": 1}
    assert {"age": 12, "count": 1} in mem["ages"]
    assert data["counts"]["move_up"] == 1
    assert [b["full_name"] for b in mem["birthdays"]] == ["Jad"] and mem["birthdays"][0]["turns"] == 12
    everything = summary(client_for(g["admin"].user, g["org"]))["membership"]
    assert (everything["joined"], everything["left"]) == (4, 1)


def test_events_and_attention(group, client_for):
    g, today = group, group["today"]
    camp = Event.objects.create(
        organization=g["org"],
        event_type=EventType.objects.get(organization=g["org"], name_en="Activity"),
        title="Camp",
        start_date=today + timedelta(days=5),
        end_date=today + timedelta(days=6),
        requires_consent=True,
        status="published",
    )
    camp.units.add(g["cubs"])
    EventInvitation.objects.create(event=camp, member=g["jad"], response="going")
    EventInvitation.objects.create(event=camp, member=g["lea"])
    events = summary(client_for(g["admin"].user, g["org"]))["events"]
    assert [e["title"] for e in events["upcoming"]] == ["Camp"]
    assert (events["upcoming"][0]["going"], events["upcoming"][0]["no_reply"]) == (1, 1)
    assert events["consent"][0]["count"] == 1
    assert events["replies"][0]["count"] == 1


def test_equipment_block_needs_inventory_permission(group, world, client_for):
    g, today = group, group["today"]
    tent = Item.objects.create(organization=g["org"], name_en="Tent", kind="counted", quantity=4)
    Item.objects.create(organization=g["org"], name_en="Gas", kind="consumable", quantity=2, min_stock=10)
    loan = Loan.objects.create(
        organization=g["org"],
        borrower_type="member",
        member=g["omar"],
        due_date=today - timedelta(days=3),
        checked_out_at=timezone.now(),
    )
    LoanLine.objects.create(loan=loan, item=tent, quantity=2)
    eq = summary(client_for(g["admin"].user, g["org"]))["equipment"]
    assert (eq["out_now"], eq["overdue"]) == (2, 1)
    assert eq["overdue_loans"][0]["days_late"] == 3
    assert [i["name_en"] for i in eq["low_stock"]] == ["Gas"]
    assert summary(client_for(g["karim"].user, g["org"]))["equipment"]["overdue"] == 1  # unit leaders can check out
    helper = world.member(g["org"], world.user("nour"), roles=[("assistant_leader", g["cubs"])])
    assert summary(client_for(helper.user, g["org"]))["equipment"] is None


def test_bad_range_is_400(group, client_for):
    client = client_for(group["admin"].user, group["org"])
    assert client.get("/api/v1/dashboard/summary/", {"range": "custom"}).status_code == 400
