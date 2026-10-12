import pytest

from apps.members.models import Member
from apps.units.models import UnitMembership


@pytest.fixture
def setup(world):
    org = world.org()
    cubs, scouts = world.unit(org, "Cubs"), world.unit(org, "Scouts")
    admin = world.member(org, world.user("rania"), owner=True)
    karim = world.member(org, world.user("karim"), roles=[("unit_leader", cubs)])
    kids = []
    for name in ("Jad", "Lea", "Elie"):
        m = Member.objects.create(organization=org, first_name=name)
        UnitMembership.objects.create(member=m, unit=cubs)
        kids.append(m)
    omar = Member.objects.create(organization=org, first_name="Omar")
    UnitMembership.objects.create(member=omar, unit=scouts)
    return dict(org=org, cubs=cubs, scouts=scouts, admin=admin, karim=karim, kids=kids, omar=omar)


def test_take_attendance(setup, client_for):
    client = client_for(setup["karim"].user, setup["org"])
    res = client.post(
        "/api/v1/attendance/sessions/",
        {
            "unit": setup["cubs"].pk,
            "title": "Saturday meeting",
            "date": "2026-10-10",
            "start_time": "10:00",
            "end_time": "12:30",
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    sid = res.data["id"]
    detail = client.get(f"/api/v1/attendance/sessions/{sid}/").data
    assert [r["full_name"] for r in detail["roster"]] == ["Elie", "Jad", "Lea"]
    assert detail["can_take"] is True
    jad, lea, elie = setup["kids"]
    res = client.put(
        f"/api/v1/attendance/sessions/{sid}/records/",
        {
            "records": [
                {"member": jad.pk, "status": "present"},
                {"member": lea.pk, "status": "absent"},
                {"member": elie.pk, "status": "excused", "note": "Sick"},
            ]
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    assert res.data["counts"] == {"present": 1, "absent": 1, "excused": 1}
    # outsider can't be marked
    bad = client.put(
        f"/api/v1/attendance/sessions/{sid}/records/",
        {"records": [{"member": setup["omar"].pk, "status": "present"}]},
        format="json",
    )
    assert bad.status_code == 400
    # clearing a mark
    client.put(
        f"/api/v1/attendance/sessions/{sid}/records/", {"records": [{"member": elie.pk, "status": None}]}, format="json"
    )
    summary = client_for(setup["admin"].user, setup["org"]).get(f"/api/v1/members/{jad.pk}/attendance/").data
    assert summary["rate"] == 100 and summary["recent"][0]["status"] == "present"
    trend = client.get(f"/api/v1/attendance/sessions/trend/?unit={setup['cubs'].pk}").data
    assert trend["points"][0]["rate"] == 50


def test_attendance_permissions(setup, client_for):
    client = client_for(setup["karim"].user, setup["org"])
    res = client.post(
        "/api/v1/attendance/sessions/", {"unit": setup["scouts"].pk, "title": "x", "date": "2026-10-10"}, format="json"
    )
    assert res.status_code == 403
    admin = client_for(setup["admin"].user, setup["org"])
    sid = admin.post(
        "/api/v1/attendance/sessions/", {"unit": setup["scouts"].pk, "title": "x", "date": "2026-10-10"}, format="json"
    ).data["id"]
    assert client.get(f"/api/v1/attendance/sessions/{sid}/").status_code == 404
    assert client.get("/api/v1/attendance/sessions/").data["count"] == 0
    assert (
        admin.post(
            "/api/v1/attendance/sessions/",
            {"unit": setup["cubs"].pk, "title": "x", "date": "2026-10-10", "start_time": "12:00", "end_time": "10:00"},
            format="json",
        ).status_code
        == 400
    )
