import pytest
from django.utils import timezone

from apps.events.models import Event
from apps.events.services import create_default_event_types
from apps.inventory.models import Item, ItemLog, Loan, Piece
from apps.members.models import Guardian, GuardianLink, Member
from apps.permissions.models import Permission, PermissionGrant
from apps.units.models import UnitMembership

URL = "/api/v1/inventory"


@pytest.fixture
def setup(world, client_for):
    org = world.org()
    create_default_event_types(org)
    cubs, scouts = world.unit(org, "Cubs"), world.unit(org, "Scouts")
    admin = world.member(org, world.user("rania"), owner=True)
    karim = world.member(org, world.user("karim"), roles=[("unit_leader", cubs)])
    joseph = world.member(org, world.user("joseph"), roles=[("quartermaster", None)])
    lina = world.member(org, world.user("lina"), roles=[("assistant_leader", scouts)])
    maya = world.member(org, world.user("maya"), roles=[("parent", None)])
    rami = Member.objects.create(organization=org, first_name="Rami", last_name="Bitar")
    UnitMembership.objects.create(member=rami, unit=scouts)
    guardian = Guardian.objects.create(organization=org, first_name="Maya", membership=maya)
    GuardianLink.objects.create(member=rami, guardian=guardian)
    qm = client_for(joseph.user, org)
    tent = qm.post(
        f"{URL}/items/", {"name_en": "Tent", "kind": "tracked", "code_prefix": "tnt", "pieces_count": 4}, format="json"
    ).data
    tins = qm.post(f"{URL}/items/", {"name_en": "Mess tins", "kind": "counted", "quantity": 10}, format="json").data
    gas = qm.post(
        f"{URL}/items/", {"name_en": "Gas", "kind": "consumable", "quantity": 6, "min_stock": 5}, format="json"
    ).data
    return dict(
        org=org, cubs=cubs, scouts=scouts, admin=admin, karim=karim, joseph=joseph, lina=lina, maya=maya,
        rami=rami, tent=tent, tins=tins, gas=gas, qm=qm,
    )  # fmt: skip


def piece_ids(item):
    return [p["id"] for p in item["pieces"]]


def test_items_and_pieces(setup):
    tent = setup["tent"]
    assert [p["code"] for p in tent["pieces"]] == ["TNT-01", "TNT-02", "TNT-03", "TNT-04"]
    assert tent["status"] == {"total": 4, "out": 0, "available": 4, "needs_care": 0, "low": False}
    items = {i["name_en"]: i for i in setup["qm"].get(f"{URL}/items/").data}
    assert items["Gas"]["status"]["low"] is False
    assert (
        setup["qm"].post(f"{URL}/items/{setup['gas']['id']}/stock/", {"action": "use", "quantity": 2}).status_code
        == 200
    )
    low = setup["qm"].get(f"{URL}/items/?low_stock=1").data
    assert [i["name_en"] for i in low] == ["Gas"]
    # Changing the kind isn't allowed; counted quantities change through stock actions.
    res = setup["qm"].patch(f"{URL}/items/{setup['tins']['id']}/", {"kind": "tracked"}, format="json")
    assert res.status_code == 400


def test_permissions_role_and_per_user(setup, client_for):
    # A unit leader's role is for the Cubs, but the store is shared: view + check out anywhere.
    karim = client_for(setup["karim"].user, setup["org"])
    assert karim.get(f"{URL}/items/").status_code == 200
    assert karim.post(f"{URL}/items/", {"name_en": "X", "kind": "counted"}, format="json").status_code == 403
    # Assistant leaders have no inventory rights until given one directly.
    lina = client_for(setup["lina"].user, setup["org"])
    assert lina.get(f"{URL}/items/").status_code == 403
    loan = {
        "borrower_type": "unit",
        "unit": setup["scouts"].pk,
        "lines": [{"item": setup["tins"]["id"], "quantity": 2}],
    }
    assert lina.post(f"{URL}/loans/", loan, format="json").status_code == 403
    PermissionGrant.objects.create(
        membership=setup["lina"],
        permission=Permission.objects.get(codename="inventory.check_out"),
        unit=setup["scouts"],
    )
    lina = client_for(setup["lina"].user, setup["org"])
    assert lina.post(f"{URL}/loans/", loan, format="json").status_code == 201
    me = lina.get("/api/v1/me/permissions/").data
    granted = {p["codename"]: p for p in me["permissions"]}
    assert granted["inventory.check_out"]["org_wide"] is True
    assert client_for(setup["maya"].user, setup["org"]).get(f"{URL}/items/").status_code == 403


def test_check_out_and_return_with_who_and_when(setup):
    qm = setup["qm"]
    tent = setup["tent"]
    t1, t2, *_ = piece_ids(tent)
    # Members and external borrowers need a due date.
    no_due = qm.post(
        f"{URL}/loans/",
        {"borrower_type": "member", "member": setup["rami"].pk, "lines": [{"item": tent["id"], "pieces": [t1]}]},
        format="json",
    )
    assert no_due.status_code == 400 and "due_date" in no_due.data["errors"]
    res = qm.post(
        f"{URL}/loans/",
        {
            "borrower_type": "external",
            "new_external": {
                "name": "2nd Beirut Scout Group",
                "contact_person": "Fadi Nasr",
                "phone": "+961 3 448 120",
            },
            "due_date": "2030-10-25",
            "note": "Autumn camp",
            "lines": [
                {"item": tent["id"], "pieces": [t1, t2]},
                {"item": setup["tins"]["id"], "quantity": 4},
                {"item": setup["gas"]["id"], "quantity": 2},
            ],
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    loan = res.data
    assert loan["borrower"]["name"] == "2nd Beirut Scout Group" and loan["checked_out_by"] == "joseph"
    assert loan["checked_out_at"] and loan["closed_at"] is None
    assert Item.objects.get(pk=setup["gas"]["id"]).quantity == 4  # consumables leave stock
    # The same piece can't go out twice; more than is in the store is refused.
    again = qm.post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": tent["id"], "pieces": [t1]}]},
        format="json",
    )
    assert again.status_code == 400
    too_many = qm.post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": setup["tins"]["id"], "quantity": 7}]},
        format="json",
    )
    assert too_many.status_code == 400
    lines = {(line["item_name_en"], line["piece_code"]): line for line in loan["lines"]}
    # Partial return: one tent back damaged, 3 tins back and 1 lost; the second tent stays out.
    res = qm.post(
        f"{URL}/loans/{loan['id']}/return/",
        {
            "returned_by_name": "Fadi Nasr",
            "returns": [
                {
                    "line": lines[("Tent", "TNT-01")]["id"],
                    "returned": 1,
                    "condition": "damaged",
                    "note": "Torn flysheet",
                },
                {"line": lines[("Mess tins", "")]["id"], "returned": 3, "lost": 1},
            ],
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    back = {(line["item_name_en"], line["piece_code"]): line for line in res.data["lines"]}
    assert (
        back[("Tent", "TNT-01")]["checked_in_by"] == "joseph"
        and back[("Tent", "TNT-01")]["returned_by_name"] == "Fadi Nasr"
    )
    assert back[("Tent", "TNT-01")]["checked_in_at"] is not None
    assert res.data["closed_at"] is None
    assert Piece.objects.get(pk=t1).condition == "damaged"
    assert Item.objects.get(pk=setup["tins"]["id"]).quantity == 9  # the lost tin is written off
    # Returning the last tent closes the loan; a missing piece is recorded as lost.
    res = qm.post(
        f"{URL}/loans/{loan['id']}/return/",
        {"returns": [{"line": lines[("Tent", "TNT-02")]["id"], "lost": 1, "note": "Not found"}]},
        format="json",
    )
    assert res.data["closed_at"] is not None
    assert Piece.objects.get(pk=t2).condition == "lost"
    detail = qm.get(f"{URL}/items/{tent['id']}/").data
    assert detail["status"]["total"] == 3 and detail["status"]["needs_care"] == 1
    kinds = [log["kind"] for log in qm.get(f"{URL}/items/{tent['id']}/history/").data]
    assert {"checked_out", "returned", "damaged", "lost"} <= set(kinds)
    # A damaged piece can still be lent, with a soft warning.
    res = qm.post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": tent["id"], "pieces": [t1]}]},
        format="json",
    )
    assert res.status_code == 201 and res.data["warnings"]
    # ...but a lost piece can't.
    lost = qm.post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": tent["id"], "pieces": [t2]}]},
        format="json",
    )
    assert lost.status_code == 400


def test_event_gear_reservations(setup, client_for):
    admin = client_for(setup["admin"].user, setup["org"])
    types = admin.get("/api/v1/event-types/").data

    def event(title, start, end):
        return admin.post(
            "/api/v1/events/",
            {
                "event_type": types[0]["id"],
                "title": title,
                "start_date": start,
                "end_date": end,
                "units": [],
                "publish": True,
            },
            format="json",
        ).data

    camp = event("Camp", "2030-10-23", "2030-10-25")
    hike = event("Hike", "2030-10-24", "2030-10-24")
    tent = setup["tent"]
    t1, t2, t3, t4 = piece_ids(tent)
    res = admin.post(f"/api/v1/events/{camp['id']}/gear/", {"item": tent["id"], "pieces": [t1, t2]}, format="json")
    assert res.status_code == 200 and res.data["rows"][0]["ok"] is True
    admin.post(f"/api/v1/events/{camp['id']}/gear/", {"item": setup["tins"]["id"], "quantity": 8}, format="json")
    # The overlapping hike wants 3 tents: only 2 are free on that day -> a warning, but it's saved.
    res = admin.post(f"/api/v1/events/{hike['id']}/gear/", {"item": tent["id"], "quantity": 3}, format="json")
    row = res.data["rows"][0]
    assert res.status_code == 200 and row["ok"] is False and row["free"] == 2
    res = admin.post(f"/api/v1/events/{hike['id']}/gear/", {"item": setup["tins"]["id"], "quantity": 3}, format="json")
    assert {r["name_en"]: r for r in res.data["rows"]}["Mess tins"]["ok"] is False
    # Leaders without inventory rights who manage the event can edit its gear list.
    karim = client_for(setup["karim"].user, setup["org"])
    assert karim.get(f"/api/v1/events/{camp['id']}/gear/").status_code == 200
    # Check out everything for the camp in one step: due back at the end of the event.
    res = setup["qm"].post(f"/api/v1/events/{camp['id']}/gear/check-out/")
    assert res.status_code == 200, res.data
    loan = Loan.objects.get(pk=res.data["loan"])
    assert loan.borrower_type == "event" and str(loan.due_date) == "2030-10-25"
    assert sorted(line.piece_id for line in loan.lines.filter(piece__isnull=False)) == [t1, t2]
    assert all(r["checked_out"] for r in res.data["rows"])
    assert setup["qm"].post(f"/api/v1/events/{camp['id']}/gear/check-out/").status_code == 400
    # Lending more tins than are in the store is refused; with 2 it works.
    assert setup["qm"].post(f"/api/v1/events/{hike['id']}/gear/check-out/").status_code == 400
    admin.post(f"/api/v1/events/{hike['id']}/gear/", {"item": setup["tins"]["id"], "quantity": 2}, format="json")
    # The hike's unassigned tents are picked from the free ones.
    res = setup["qm"].post(f"/api/v1/events/{hike['id']}/gear/check-out/")
    hike_loan = Loan.objects.get(pk=res.data["loan"])
    assert sorted(line.piece_id for line in hike_loan.lines.filter(item_id=tent["id"])) == [t3, t4]
    assert Event.objects.get(pk=camp["id"]).loans.count() == 1


def test_stocktake_overdue_lookup_and_my_gear(setup, client_for):
    qm = setup["qm"]
    st = qm.post(f"{URL}/stocktakes/", {}, format="json").data
    lines = {(line["item_name_en"], line["piece_code"]): line for line in st["lines"]}
    assert lines[("Mess tins", "")]["expected"] == 10
    res = qm.patch(
        f"{URL}/stocktakes/{st['id']}/",
        {"counts": {str(lines[("Mess tins", "")]["id"]): 9, str(lines[("Tent", "TNT-01")]["id"]): 0}},
        format="json",
    )
    assert res.data["differences"] == 2
    qm.post(f"{URL}/stocktakes/{st['id']}/finish/")
    assert Item.objects.get(pk=setup["tins"]["id"]).quantity == 9
    assert ItemLog.objects.filter(item_id=setup["tent"]["id"], kind="adjusted").exists()
    assert qm.post(f"{URL}/stocktakes/{st['id']}/finish/").status_code == 400

    t1 = piece_ids(setup["tent"])[0]
    qm.post(
        f"{URL}/loans/",
        {
            "borrower_type": "member",
            "member": setup["rami"].pk,
            "due_date": str(timezone.localdate() - timezone.timedelta(days=2)),
            "lines": [{"item": setup["tent"]["id"], "pieces": [t1]}],
        },
        format="json",
    )
    summary = qm.get(f"{URL}/summary/").data
    assert summary["overdue"] == 1 and summary["out_now"] == 1
    assert summary["overdue_loans"][0]["overdue"] is True
    assert qm.get(f"{URL}/loans/?status=overdue").data["count"] == 1
    assert qm.get(f"{URL}/lookup/?code=tnt-01").data == {"item": setup["tent"]["id"], "piece": t1}
    assert qm.get(f"{URL}/lookup/?code=item-{setup['tins']['id']}").data["item"] == setup["tins"]["id"]
    assert qm.get(f"{URL}/lookup/?code=nope").status_code == 404
    mine = client_for(setup["maya"].user, setup["org"]).get("/api/v1/my/gear/").data
    assert mine["open"][0]["lines"][0]["piece_code"] == "TNT-01"


def test_tenant_isolation_and_org_deletion(setup, world, client_for):
    other = world.org("2nd Beirut")
    stranger = world.member(other, world.user("stranger"), owner=True)
    client = client_for(stranger.user, other)
    assert client.get(f"{URL}/items/").data == []
    assert client.get(f"{URL}/items/{setup['tent']['id']}/").status_code == 404
    res = client.post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": setup["tins"]["id"], "quantity": 1}]},
        format="json",
    )
    assert res.status_code == 400
    setup["qm"].post(
        f"{URL}/loans/",
        {"borrower_type": "unit", "unit": setup["cubs"].pk, "lines": [{"item": setup["tins"]["id"], "quantity": 1}]},
        format="json",
    )
    setup["org"].delete()
    assert not Item.objects.exists() and not Loan.objects.exists()


def test_borrower_options_need_check_out(setup, client_for):
    data = setup["qm"].get(f"{URL}/borrowers/?search=ram").data
    assert [m["full_name"] for m in data["members"]] == ["Rami Bitar"]
    assert client_for(setup["lina"].user, setup["org"]).get(f"{URL}/borrowers/").status_code == 403
