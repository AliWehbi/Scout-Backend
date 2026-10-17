import pytest

from apps.events.models import Event, EventType
from apps.events.services import create_default_event_types
from apps.members.models import Guardian, GuardianLink, Member
from apps.units.models import UnitMembership


@pytest.fixture
def setup(world):
    org = world.org()
    create_default_event_types(org)
    cubs, scouts = world.unit(org, "Cubs"), world.unit(org, "Scouts")
    admin = world.member(org, world.user("rania"), owner=True)
    karim = world.member(org, world.user("karim"), roles=[("unit_leader", cubs)])
    maya = world.member(org, world.user("maya"), roles=[("parent", None)])
    hadi_login = world.member(org, world.user("hadi"), roles=[("scout", None)])

    def kid(name, unit, membership=None):
        m = Member.objects.create(organization=org, first_name=name, membership=membership)
        UnitMembership.objects.create(member=m, unit=unit)
        return m

    jad, lea = kid("Jad", cubs), kid("Lea", cubs)
    hadi = kid("Hadi", cubs, hadi_login)
    omar = kid("Omar", scouts)
    guardian = Guardian.objects.create(organization=org, first_name="Maya", last_name="Saliba", membership=maya)
    GuardianLink.objects.create(member=jad, guardian=guardian, relationship="mother")
    meeting = EventType.objects.get(organization=org, name_en="Meeting")
    activity = EventType.objects.get(organization=org, name_en="Activity")
    return dict(
        org=org, cubs=cubs, scouts=scouts, admin=admin, karim=karim, maya=maya, hadi_login=hadi_login,
        jad=jad, lea=lea, hadi=hadi, omar=omar, meeting=meeting, activity=activity,
    )  # fmt: skip


def event_payload(s, **extra):
    return {
        "event_type": s["activity"].pk,
        "title": "Autumn camp",
        "start_date": "2030-10-23",
        "start_time": "16:00",
        "end_date": "2030-10-25",
        "end_time": "14:00",
        "units": [s["cubs"].pk],
        "is_mandatory": True,
        "publish": True,
        **extra,
    }


def create_event(client, s, **extra):
    res = client.post("/api/v1/events/", event_payload(s, **extra), format="json")
    assert res.status_code == 201, res.data
    return res.data


def test_default_types_and_type_rules(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    names = [t["name_en"] for t in admin.get("/api/v1/event-types/").data]
    assert names == ["Meeting", "Activity"]
    res = admin.post(
        "/api/v1/event-types/",
        {"name_en": "Camp", "name_ar": "مخيم", "color": "info", "icon": "camping", "default_mandatory": True},
        format="json",
    )
    assert res.status_code == 201, res.data
    assert admin.post("/api/v1/event-types/", {"name_en": "camp", "icon": "groups"}, format="json").status_code == 400
    parent = client_for(setup["maya"].user, setup["org"])
    assert parent.get("/api/v1/event-types/").status_code == 200
    assert parent.post("/api/v1/event-types/", {"name_en": "X", "icon": "groups"}, format="json").status_code == 403
    create_event(admin, setup)
    used = admin.delete(f"/api/v1/event-types/{setup['activity'].pk}/")
    assert used.status_code == 400
    assert (
        admin.patch(f"/api/v1/event-types/{setup['activity'].pk}/", {"is_archived": True}, format="json").status_code
        == 200
    )
    archived = admin.post("/api/v1/events/", event_payload(setup), format="json")
    assert archived.status_code == 400 and "event_type" in archived.data["errors"]


def test_manage_permissions_follow_units(setup, client_for):
    karim = client_for(setup["karim"].user, setup["org"])
    create_event(karim, setup)
    assert (
        karim.post("/api/v1/events/", event_payload(setup, units=[setup["scouts"].pk]), format="json").status_code
        == 403
    )
    assert karim.post("/api/v1/events/", event_payload(setup, units=[]), format="json").status_code == 403
    admin = client_for(setup["admin"].user, setup["org"])
    scouts_event = create_event(admin, setup, units=[setup["scouts"].pk])
    org_wide = create_event(admin, setup, units=[])
    visible = {e["id"] for e in karim.get("/api/v1/events/").data["results"]}
    assert org_wide["id"] in visible and scouts_event["id"] not in visible
    assert karim.patch(f"/api/v1/events/{org_wide['id']}/", {"title": "x"}, format="json").status_code == 403
    parent = client_for(setup["maya"].user, setup["org"])
    assert parent.get("/api/v1/events/").data["results"] == []
    assert parent.post("/api/v1/events/", event_payload(setup), format="json").status_code == 403


def test_validation_and_repeat_weekly(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    bad = admin.post("/api/v1/events/", event_payload(setup, end_date="2030-10-20"), format="json")
    assert bad.status_code == 400 and "end_date" in bad.data["errors"]
    no_text = admin.post("/api/v1/events/", event_payload(setup, requires_consent=True), format="json")
    assert no_text.status_code == 400 and "consent_text" in no_text.data["errors"]
    series = create_event(
        admin,
        setup,
        event_type=setup["meeting"].pk,
        title="Cubs meeting",
        start_date="2030-09-07",
        end_date="2030-09-07",
        end_time="12:00",
        start_time="10:00",
        repeat_until="2030-10-05",
        reply_deadline="2030-09-06",
    )
    assert series["created_count"] == 5
    events = Event.objects.filter(series=series["series"]).order_by("start_date")
    assert [str(e.start_date) for e in events][-1] == "2030-10-05"
    assert str(events.last().reply_deadline) == "2030-10-04"  # the same "day before" every week
    assert all(list(e.units.all()) == [setup["cubs"]] for e in events)


def test_invitations_replies_and_waitlist(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    event = create_event(admin, setup, capacity=1, title="Football", is_mandatory=False)
    res = admin.post(
        f"/api/v1/events/{event['id']}/invite/", {"units": [setup["cubs"].pk], "include_parents": False}, format="json"
    )
    assert res.data["added"] == 3
    # Parents aren't invited, so Maya doesn't see it; Hadi (own login) does.
    parent = client_for(setup["maya"].user, setup["org"])
    assert parent.get("/api/v1/my/events/").data == []
    hadi = client_for(setup["hadi_login"].user, setup["org"])
    mine = hadi.get("/api/v1/my/events/").data
    assert len(mine) == 1 and mine[0]["people"][0]["is_me"] and mine[0]["people"][0]["can_reply"]
    hadi_inv = mine[0]["people"][0]["invitation"]
    assert hadi.post(f"/api/v1/my/events/invitations/{hadi_inv}/respond/", {"response": "going"}).status_code == 200

    # Now include parents for Jad; Maya replies, and the event is full -> waitlist.
    admin.post(
        f"/api/v1/events/{event['id']}/invite/", {"members": [setup["jad"].pk], "include_parents": True}, format="json"
    )
    rows = {r["full_name"]: r for r in admin.get(f"/api/v1/events/{event['id']}/invitations/").data}
    jad_inv = rows["Jad"]["id"]
    assert rows["Jad"]["include_parents"] is False  # already invited, unchanged
    admin.post(f"/api/v1/events/{event['id']}/invite/", {"members": [setup["jad"].pk]}, format="json")
    from apps.events.models import EventInvitation

    EventInvitation.objects.filter(pk=jad_inv).update(include_parents=True)
    res = parent.post(f"/api/v1/my/events/invitations/{jad_inv}/respond/", {"response": "going"})
    assert res.status_code == 200, res.data
    person = res.data["people"][0]
    assert person["waitlisted"] is True and person["waitlist_position"] == 1
    # Hadi drops out -> Jad moves up.
    hadi.post(f"/api/v1/my/events/invitations/{hadi_inv}/respond/", {"response": "not_going"})
    rows = {r["full_name"]: r for r in admin.get(f"/api/v1/events/{event['id']}/invitations/").data}
    assert rows["Jad"]["waitlisted"] is False and rows["Jad"]["responded_as"] == "parent"
    detail = admin.get(f"/api/v1/events/{event['id']}/").data
    assert detail["counts"]["going"] == 1 and detail["counts"]["not_going"] == 1 and detail["counts"]["no_reply"] == 1
    # A leader replies on Lea's behalf.
    lea_inv = rows["Lea"]["id"]
    res = admin.post(f"/api/v1/events/{event['id']}/invitations/{lea_inv}/respond/", {"response": "maybe"})
    assert {r["full_name"]: r for r in res.data}["Lea"]["responded_as"] == "leader"
    # Maya can't answer for a child that isn't hers.
    assert parent.post(f"/api/v1/my/events/invitations/{lea_inv}/respond/", {"response": "going"}).status_code == 403


def test_consent_signatures_and_resign(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    event = create_event(admin, setup, requires_consent=True, consent_text="I allow my child to attend.")
    admin.post(
        f"/api/v1/events/{event['id']}/invite/", {"members": [setup["jad"].pk], "include_parents": False}, format="json"
    )
    parent = client_for(setup["maya"].user, setup["org"])
    mine = parent.get("/api/v1/my/events/").data  # signature forces parents in
    inv = mine[0]["people"][0]["invitation"]
    parent.post(f"/api/v1/my/events/invitations/{inv}/respond/", {"response": "going"})
    assert parent.get("/api/v1/my/family/").data["events_pending"] == 1
    hadi = client_for(setup["hadi_login"].user, setup["org"])
    assert hadi.post(f"/api/v1/my/events/invitations/{inv}/sign/", {"name": "Hadi", "agree": True}).status_code == 403
    assert (
        parent.post(f"/api/v1/my/events/invitations/{inv}/sign/", {"name": "Maya", "agree": False}).status_code == 400
    )
    res = parent.post(f"/api/v1/my/events/invitations/{inv}/sign/", {"name": "Maya  Saliba", "agree": True})
    assert res.status_code == 200, res.data
    assert res.data["people"][0]["signature"]["name"] == "Maya Saliba"
    assert parent.get("/api/v1/my/family/").data["events_pending"] == 0
    # Editing the text without asking to sign again keeps the signature...
    admin.patch(f"/api/v1/events/{event['id']}/", {"consent_text": "Updated text."}, format="json")
    assert admin.get(f"/api/v1/events/{event['id']}/").data["counts"]["signed"] == 1
    # ...asking to sign again clears it; the old signature keeps the text that was signed.
    admin.patch(f"/api/v1/events/{event['id']}/", {"consent_text": "New text.", "resign_required": True}, format="json")
    assert admin.get(f"/api/v1/events/{event['id']}/").data["counts"]["signed"] == 0
    from apps.events.models import ConsentSignature

    assert ConsentSignature.objects.get().text == "I allow my child to attend."
    assert parent.get("/api/v1/my/events/").data[0]["people"][0]["can_sign"] is True


def test_check_in_per_day_and_report(setup, client_for):
    karim = client_for(setup["karim"].user, setup["org"])
    event = create_event(
        karim, setup, start_date="2026-01-09", end_date="2026-01-11", requires_consent=True, consent_text="ok"
    )
    karim.post(f"/api/v1/events/{event['id']}/invite/", {"units": [setup["cubs"].pk]}, format="json")
    rows = {r["full_name"]: r for r in karim.get(f"/api/v1/events/{event['id']}/invitations/").data}
    karim.post(f"/api/v1/events/{event['id']}/invitations/{rows['Jad']['id']}/respond/", {"response": "going"})
    day1 = karim.get(f"/api/v1/events/{event['id']}/attendance/?date=2026-01-09").data
    assert day1["days"] == ["2026-01-09", "2026-01-10", "2026-01-11"] and day1["session"] is None
    jad_row = next(r for r in day1["roster"] if r["full_name"] == "Jad")
    assert jad_row["missing_signature"] is True  # soft warning only
    jad, lea, hadi = setup["jad"], setup["lea"], setup["hadi"]
    url = f"/api/v1/events/{event['id']}/attendance/"
    res = karim.put(
        url,
        {
            "date": "2026-01-09",
            "records": [
                {"member": jad.pk, "status": "present", "note": "ignored"},
                {"member": lea.pk, "status": "excused", "note": "Sick"},
                {"member": hadi.pk, "status": "absent"},
            ],
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    roster = {r["full_name"]: r for r in res.data["roster"]}
    assert roster["Jad"]["note"] == "" and roster["Lea"]["note"] == "Sick"
    karim.put(url, {"date": "2026-01-10", "records": [{"member": jad.pk, "status": "present"}]}, format="json")
    bad = karim.put(url, {"date": "2026-01-12", "records": []}, format="json")
    assert bad.status_code == 400
    outsider = karim.put(
        url, {"date": "2026-01-09", "records": [{"member": setup["omar"].pk, "status": "present"}]}, format="json"
    )
    assert outsider.status_code == 400
    # Event days don't show up as standalone sessions...
    assert karim.get(f"/api/v1/attendance/sessions/?unit={setup['cubs'].pk}").data["results"] == []
    # ...but count in reports, together with an optional standalone session.
    sid = karim.post(
        "/api/v1/attendance/sessions/",
        {"unit": setup["cubs"].pk, "title": "Extra", "date": "2026-01-14", "is_mandatory": False},
        format="json",
    ).data["id"]
    karim.put(
        f"/api/v1/attendance/sessions/{sid}/records/",
        {"records": [{"member": jad.pk, "status": "absent"}]},
        format="json",
    )
    report = karim.get(f"/api/v1/attendance/report/?unit={setup['cubs'].pk}&mandatory=true").data
    assert report["summary"]["held"] == 2 and report["summary"]["events"] == 2 and report["summary"]["hidden"] == 1
    members = {m["full_name"]: m for m in report["members"]}
    assert members["Jad"]["present"] == 2 and members["Jad"]["rate"] == 100
    assert members["Lea"]["excused"] == 1 and members["Lea"]["rate"] is None
    everything = karim.get(f"/api/v1/attendance/report/?unit={setup['cubs'].pk}").data
    assert {m["full_name"]: m for m in everything["members"]}["Jad"]["rate"] == 67
    only_sessions = karim.get(f"/api/v1/attendance/report/?unit={setup['cubs'].pk}&source=sessions").data
    assert only_sessions["summary"]["held"] == 1
    assert karim.get(f"/api/v1/attendance/report/?unit={setup['scouts'].pk}").status_code == 403
    # Mandatory flag changes follow the event; attendance blocks deletion.
    karim.patch(f"/api/v1/events/{event['id']}/", {"is_mandatory": False}, format="json")
    assert karim.get(f"/api/v1/attendance/report/?unit={setup['cubs'].pk}&mandatory=false").data["summary"]["held"] == 3
    assert karim.delete(f"/api/v1/events/{event['id']}/").status_code == 400
    history = client_for(setup["admin"].user, setup["org"]).get(f"/api/v1/members/{lea.pk}/attendance/").data
    assert history["recent"][0]["note"] == "Sick" and history["recent"][0]["event_id"] == event["id"]


def test_tenant_isolation(setup, world, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    event = create_event(admin, setup)
    admin.post(f"/api/v1/events/{event['id']}/invite/", {"units": [setup["cubs"].pk]}, format="json")
    inv = admin.get(f"/api/v1/events/{event['id']}/invitations/").data[0]["id"]
    other = world.org("2nd Beirut")
    stranger = world.member(other, world.user("stranger"), owner=True)
    client = client_for(stranger.user, other)
    assert client.get(f"/api/v1/events/{event['id']}/").status_code == 404
    assert client.get("/api/v1/events/").data["results"] == []
    assert client.post(f"/api/v1/my/events/invitations/{inv}/respond/", {"response": "going"}).status_code == 403
    assert client.get("/api/v1/event-types/").data == []
    bad_type = client.post("/api/v1/events/", {**event_payload(setup), "units": []}, format="json")
    assert bad_type.status_code == 400 and "event_type" in bad_type.data["errors"]


def test_calendar_feed(setup, client_for, anon):
    admin = client_for(setup["admin"].user, setup["org"])
    create_event(admin, setup, title="Camp, Ehden; night", start_date="2026-01-09", end_date="2026-01-11")
    create_event(
        admin, setup, title="Draft one", publish=False, start_date="2026-01-09", end_date="2026-01-09", end_time="18:00"
    )
    token = admin.get("/api/v1/my/calendar/").data["token"]
    assert admin.get("/api/v1/my/calendar/").data["token"] == token
    res = anon.get(f"/api/v1/calendar/{token}.ics")
    assert res.status_code == 200 and res["Content-Type"].startswith("text/calendar")
    body = res.content.decode()
    assert "SUMMARY:Camp\\, Ehden\\; night" in body and "Draft one" not in body
    assert "DTSTART;TZID=UTC:20260109T160000" in body
    new = admin.post("/api/v1/my/calendar/").data["token"]
    assert anon.get(f"/api/v1/calendar/{token}.ics").status_code == 404
    assert anon.get(f"/api/v1/calendar/{new}.ics").status_code == 200


def test_organization_with_events_can_be_deleted(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    create_event(admin, setup)
    setup["org"].delete()
    assert not Event.objects.exists() and not EventType.objects.exists()


def test_org_wide_event_counts_per_unit(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    event = create_event(admin, setup, start_date="2026-02-07", end_date="2026-02-07", end_time="18:00", units=[])
    admin.post(
        f"/api/v1/events/{event['id']}/invite/", {"units": [setup["cubs"].pk, setup["scouts"].pk]}, format="json"
    )
    res = admin.put(
        f"/api/v1/events/{event['id']}/attendance/",
        {
            "date": "2026-02-07",
            "records": [
                {"member": setup["jad"].pk, "status": "present"},
                {"member": setup["omar"].pk, "status": "absent"},
            ],
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    # A leader of one unit sees the event, counted over their unit's members only.
    karim = client_for(setup["karim"].user, setup["org"])
    report = karim.get(f"/api/v1/attendance/report/?units={setup['cubs'].pk}").data
    assert report["summary"]["events"] == 1 and report["occasions"][0]["rate"] == 100
    members = {m["full_name"]: m for m in report["members"]}
    assert members["Jad"]["present"] == 1 and "Omar" not in members
    assert karim.get(f"/api/v1/attendance/sessions/trend/?unit={setup['cubs'].pk}").data["points"][0]["rate"] == 100
    assert karim.get(f"/api/v1/attendance/report/?units={setup['cubs'].pk},{setup['scouts'].pk}").status_code == 403
    # Several units at once.
    both = admin.get(f"/api/v1/attendance/report/?units={setup['cubs'].pk},{setup['scouts'].pk}").data
    assert both["occasions"][0]["rate"] == 50
    assert {m["full_name"] for m in both["members"]} >= {"Jad", "Omar"}


def test_excused_counted_as_absent(setup, client_for):
    karim = client_for(setup["karim"].user, setup["org"])
    cubs = setup["cubs"].pk
    for day, status in (("2026-03-07", "present"), ("2026-03-14", "excused")):
        sid = karim.post(
            "/api/v1/attendance/sessions/", {"unit": cubs, "title": "Meeting", "date": day}, format="json"
        ).data["id"]
        karim.put(
            f"/api/v1/attendance/sessions/{sid}/records/",
            {"records": [{"member": setup["jad"].pk, "status": status, "note": "Sick"}]},
            format="json",
        )
    lenient = karim.get(f"/api/v1/attendance/report/?units={cubs}").data
    strict = karim.get(f"/api/v1/attendance/report/?units={cubs}&excused=absent").data
    assert lenient["summary"]["average"] == 100 and strict["summary"]["average"] == 50
    assert {m["full_name"]: m for m in strict["members"]}["Jad"]["rate"] == 50
    assert [
        p["rate"] for p in karim.get(f"/api/v1/attendance/sessions/trend/?units={cubs}&excused=absent").data["points"]
    ] == [100, 0]
    admin = client_for(setup["admin"].user, setup["org"])
    jad = setup["jad"].pk
    summary = admin.get(f"/api/v1/members/{jad}/attendance/").data
    assert summary["rate"] == 100 and summary["rate_strict"] == 50
    # Parents see both numbers too.
    child = client_for(setup["maya"].user, setup["org"]).get("/api/v1/my/family/children/").data[0]
    assert child["attendance"]["rate"] == 100 and child["attendance"]["rate_strict"] == 50


def test_closing_locks_the_event(setup, world, client_for):
    from apps.inventory.models import Item

    org = setup["org"]
    karim_member = Member.objects.create(organization=org, first_name="Karim", membership=setup["karim"])
    other = world.member(org, world.user("nour"), roles=[("unit_leader", setup["cubs"])])
    other_member = Member.objects.create(organization=org, first_name="Nour", membership=other)
    karim, nour = client_for(setup["karim"].user, org), client_for(other.user, org)
    admin, parent = client_for(setup["admin"].user, org), client_for(setup["maya"].user, org)
    event = create_event(
        karim, setup, requires_consent=True, consent_text="ok", leaders=[karim_member.pk, other_member.pk]
    )
    url = f"/api/v1/events/{event['id']}"
    karim.post(f"{url}/invite/", {"members": [setup["jad"].pk, setup["lea"].pk]}, format="json")
    rows = {r["full_name"]: r for r in karim.get(f"{url}/invitations/").data}
    jad_inv, lea_inv = rows["Jad"]["id"], rows["Lea"]["id"]
    parent.post(f"/api/v1/my/events/invitations/{jad_inv}/respond/", {"response": "going"})

    # Only the event's own leaders close it: not even the owner, who isn't listed as a leader.
    assert admin.get(f"{url}/").data["can_close"] is False
    assert admin.post(f"{url}/close/").status_code == 403
    assert karim.get(f"{url}/").data["can_close"] is True
    res = karim.post(f"{url}/close/")
    assert res.status_code == 200 and res.data["closed_at"] and res.data["closed_by_name"] == "karim"
    assert res.data["can_reopen"] is True and res.data["can_close"] is False

    # Everything is read-only now.
    tent = Item.objects.create(organization=org, name_en="Tent", kind="counted", quantity=4)
    blocked = [
        karim.patch(f"{url}/", {"title": "Changed"}, format="json"),
        karim.post(f"{url}/invite/", {"members": [setup["hadi"].pk]}, format="json"),
        karim.delete(f"{url}/invitations/{lea_inv}/"),
        karim.post(f"{url}/invitations/{lea_inv}/respond/", {"response": "maybe"}),
        karim.put(f"{url}/attendance/", {"date": event["start_date"], "records": [{"member": setup["jad"].pk, "status": "present"}]}, format="json"),
        karim.post(f"{url}/cancel/"),
        karim.delete(f"{url}/"),
        parent.post(f"/api/v1/my/events/invitations/{jad_inv}/respond/", {"response": "not_going"}),
        parent.post(f"/api/v1/my/events/invitations/{jad_inv}/sign/", {"name": "Maya", "agree": True}),
        admin.post(f"{url}/gear/", {"item": tent.pk, "quantity": 1}, format="json"),
    ]  # fmt: skip
    assert [r.status_code for r in blocked] == [400] * len(blocked)
    assert karim.get(f"{url}/attendance/").data["can_take"] is False
    mine = parent.get("/api/v1/my/events/").data[0]
    assert mine["closed"] is True and mine["people"][0]["can_reply"] is False and mine["people"][0]["can_sign"] is False
    assert admin.get(f"{url}/gear/").data["can_edit"] is False

    # Another leader of the event can't reopen it; the leader who closed it, or an admin, can.
    assert nour.get(f"{url}/").data["can_reopen"] is False
    assert nour.post(f"{url}/reopen/").status_code == 403
    assert admin.get(f"{url}/").data["can_reopen"] is True
    assert admin.post(f"{url}/reopen/").status_code == 200
    assert karim.post(f"{url}/invite/", {"members": [setup["hadi"].pk]}, format="json").status_code == 200
    karim.post(f"{url}/close/")
    res = karim.post(f"{url}/reopen/")
    assert res.status_code == 200 and res.data["closed_at"] is None
    from apps.audit.models import AuditLog

    assert AuditLog.objects.filter(action="event.closed").count() == 2
    assert AuditLog.objects.filter(action="event.reopened").count() == 2
