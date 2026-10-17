from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.events.models import Event, EventType
from apps.events.services import create_default_event_types
from apps.finance.models import Category, Currency, Entry
from apps.finance.services import create_default_finance
from apps.permissions.models import Permission, PermissionGrant

URL = "/api/v1/finance"


@pytest.fixture
def setup(world, client_for):
    org = world.org()
    create_default_event_types(org)
    create_default_finance(org)
    cubs = world.unit(org, "Cubs")
    admin = world.member(org, world.user("rania"), owner=True)
    treasurer = world.member(org, world.user("joseph"), roles=[("treasurer", cubs)])  # unit role, org-wide rights
    karim = world.member(org, world.user("karim"), roles=[("unit_leader", cubs)])
    usd = Currency.objects.get(organization=org, code="USD")
    lbp = Currency.objects.create(
        organization=org, code="LBP", name_en="Lebanese pound", name_ar="ليرة لبنانية",
        symbol_en="LBP", symbol_ar="ل.ل.", decimals=0, rate=Decimal("89500"),
    )  # fmt: skip
    cats = {c.name_en: c for c in Category.objects.filter(organization=org)}
    camp = Event.objects.create(
        organization=org, event_type=EventType.objects.filter(organization=org).first(), title="Autumn camp",
        start_date=date(2026, 10, 23), end_date=date(2026, 10, 25),
    )  # fmt: skip
    return dict(
        org=org, admin=admin, treasurer=treasurer, karim=karim, usd=usd, lbp=lbp, cats=cats, camp=camp,
        tr=client_for(treasurer.user, org),
    )  # fmt: skip


def expense(s, **extra):
    data = {
        "kind": "expense", "date": "2026-10-13", "amount": "50", "currency": s["usd"].pk,
        "category": s["cats"]["Food"].pk, "description": "Groceries",
    }  # fmt: skip
    data.update(extra)
    return data


def test_defaults_for_every_organization(setup):
    currencies = setup["tr"].get(f"{URL}/currencies/").data
    assert [(c["code"], c["is_default"]) for c in currencies] == [("USD", True), ("LBP", False)]
    assert currencies[0]["symbol_ar"] == "$" and currencies[1]["symbol_ar"] == "ل.ل."
    kinds = [c["kind"] for c in setup["tr"].get(f"{URL}/categories/").data]
    assert kinds.count("expense") == 6 and kinds.count("income") == 3


def test_default_currency_follows_the_organization(world, client_for):
    org = world.org("2nd Beirut")
    org.currency = "LBP"
    org.save()
    create_default_finance(org)
    lbp = Currency.objects.get(organization=org)
    assert (lbp.code, lbp.is_default, lbp.symbol_ar, lbp.decimals) == ("LBP", True, "ل.ل.", 0)
    admin = client_for(world.member(org, world.user("fadi"), owner=True).user, org)
    presets = {p["code"]: p for p in admin.get(f"{URL}/currencies/presets/").data}
    assert presets["EUR"]["name_ar"] == "يورو" and presets["USD"]["symbol_en"] == "$"


def test_permissions_org_wide_role_and_per_user(setup, client_for):
    org = setup["org"]
    # The treasurer role is assigned for the Cubs, but the books belong to the whole organization.
    assert setup["tr"].post(f"{URL}/entries/", expense(setup), format="json").status_code == 201
    karim = client_for(setup["karim"].user, org)
    assert karim.get(f"{URL}/entries/").status_code == 403
    assert karim.get(f"{URL}/summary/").status_code == 403
    PermissionGrant.objects.create(
        membership=setup["karim"], permission=Permission.objects.get(codename="finance.view")
    )
    karim = client_for(setup["karim"].user, org)
    assert karim.get(f"{URL}/entries/").status_code == 200
    assert karim.get(f"{URL}/summary/").status_code == 200
    assert karim.get(f"{URL}/categories/").status_code == 200
    assert [e["title"] for e in karim.get(f"{URL}/events/?search=autumn").data] == ["Autumn camp"]
    assert karim.post(f"{URL}/entries/", expense(setup), format="json").status_code == 403
    assert karim.post(f"{URL}/categories/", {"kind": "income", "name_en": "X"}, format="json").status_code == 403


def test_rates_are_kept_with_each_entry(setup):
    tr, lbp = setup["tr"], setup["lbp"]
    # Another currency starts from its current rate; the amount is kept in the default currency too.
    res = tr.post(f"{URL}/entries/", expense(setup, amount="9800000", currency=lbp.pk), format="json")
    assert res.status_code == 201, res.data
    assert res.data["rate"] == "89500.00000000" and res.data["amount_default"] == "109.4972"
    assert res.data["currency"]["symbol_ar"] == "ل.ل."
    # A rate entered by hand wins.
    res2 = tr.post(f"{URL}/entries/", expense(setup, amount="900000", currency=lbp.pk, rate="90000"), format="json")
    assert res2.data["amount_default"] == "10.0000"
    # The default currency is always 1, whatever is sent.
    res3 = tr.post(f"{URL}/entries/", expense(setup, rate="3"), format="json")
    assert res3.data["rate"] == "1.00000000" and res3.data["amount_default"] == "50.0000"
    # Changing the currency's rate later doesn't touch past entries.
    upd = tr.patch(f"{URL}/currencies/{lbp.pk}/", {"rate": "100000"}, format="json")
    assert upd.status_code == 200 and upd.data["rate_updated_by_name"] == "joseph"
    assert Entry.objects.get(pk=res.data["id"]).amount_default == Decimal("109.4972")
    # Editing only the amount keeps the entry's own rate.
    edit = tr.patch(f"{URL}/entries/{res.data['id']}/", {"amount": "179000"}, format="json")
    assert edit.data["rate"] == "89500.00000000" and edit.data["amount_default"] == "2.0000"
    # Switching currency takes that currency's rate.
    edit = tr.patch(f"{URL}/entries/{res.data['id']}/", {"currency": setup["usd"].pk}, format="json")
    assert edit.data["rate"] == "1.00000000"
    assert tr.post(f"{URL}/entries/", expense(setup, amount="0"), format="json").status_code == 400


def test_categories_and_hidden_choices(setup):
    tr, cats = setup["tr"], setup["cats"]
    res = tr.post(f"{URL}/entries/", expense(setup, category=cats["Donations"].pk), format="json")
    assert res.status_code == 400 and "category" in res.data["errors"]
    entry = tr.post(f"{URL}/entries/", expense(setup), format="json").data
    food = cats["Food"]
    assert tr.patch(f"{URL}/categories/{food.pk}/", {"is_hidden": True}, format="json").status_code == 200
    # Hidden categories can't be picked for new entries, but existing entries keep them.
    assert tr.post(f"{URL}/entries/", expense(setup), format="json").status_code == 400
    assert tr.patch(f"{URL}/entries/{entry['id']}/", {"description": "Bread"}, format="json").status_code == 200
    # Used categories are hidden, not deleted; unused ones can be deleted.
    assert tr.delete(f"{URL}/categories/{food.pk}/").status_code == 400
    assert tr.delete(f"{URL}/categories/{cats['Transport'].pk}/").status_code == 204
    # Names are unique per kind, and the kind is fixed.
    assert tr.post(f"{URL}/categories/", {"kind": "expense", "name_en": "food"}, format="json").status_code == 400
    assert tr.post(f"{URL}/categories/", {"kind": "income", "name_en": "Food"}, format="json").status_code == 201
    assert tr.patch(f"{URL}/categories/{food.pk}/", {"kind": "income"}, format="json").status_code == 400
    got = tr.get(f"{URL}/categories/?kind=income").data
    ids = [c["id"] for c in got][::-1]
    res = tr.post(f"{URL}/categories/reorder/", {"ids": ids}, format="json")
    assert [c["id"] for c in res.data if c["kind"] == "income"] == ids


def test_currencies(setup):
    tr, lbp, usd = setup["tr"], setup["lbp"], setup["usd"]
    res = tr.post(
        f"{URL}/currencies/",
        {"code": "eur", "name_en": "Euro", "name_ar": "يورو", "symbol_en": "€", "symbol_ar": "€", "rate": "0.92"},
        format="json",
    )
    assert res.status_code == 201 and res.data["code"] == "EUR"
    assert (
        tr.post(f"{URL}/currencies/", {"code": "EUR", "name_en": "x", "symbol_en": "x"}, format="json").status_code
        == 400
    )
    assert (
        tr.post(f"{URL}/currencies/", {"code": "EURO", "name_en": "x", "symbol_en": "x"}, format="json").status_code
        == 400
    )
    # The default currency stays at 1 and can't be hidden or deleted.
    assert tr.patch(f"{URL}/currencies/{usd.pk}/", {"rate": "5"}, format="json").data["rate"] == "1.00000000"
    assert tr.patch(f"{URL}/currencies/{usd.pk}/", {"is_hidden": True}, format="json").status_code == 400
    assert tr.delete(f"{URL}/currencies/{usd.pk}/").status_code == 400
    # While nothing is recorded, the default can change; the other rates follow.
    res = tr.post(f"{URL}/currencies/{lbp.pk}/make-default/")
    assert res.status_code == 200
    rates = {c["code"]: (c["is_default"], c["rate"]) for c in res.data}
    assert rates["LBP"] == (True, "1.00000000")
    assert rates["USD"] == (False, "0.00001117")
    assert rates["EUR"][1] == "0.00001028"
    setup["org"].refresh_from_db()
    assert setup["org"].currency == "LBP"  # event costs follow the books
    tr.post(f"{URL}/currencies/{usd.pk}/make-default/")
    # Once entries exist, the default is fixed; used currencies can only be hidden.
    tr.post(f"{URL}/entries/", expense(setup, currency=lbp.pk), format="json")
    assert tr.post(f"{URL}/currencies/{lbp.pk}/make-default/").status_code == 400
    assert tr.delete(f"{URL}/currencies/{lbp.pk}/").status_code == 400
    assert tr.patch(f"{URL}/currencies/{lbp.pk}/", {"is_hidden": True}, format="json").status_code == 200
    assert tr.post(f"{URL}/entries/", expense(setup, currency=lbp.pk), format="json").status_code == 400


def test_closed_events_lock_their_entries(setup):
    tr, camp = setup["tr"], setup["camp"]
    res = tr.post(f"{URL}/entries/", expense(setup, event=camp.pk), format="json")
    assert res.status_code == 201 and res.data["event"]["id"] == camp.pk
    assert tr.get(f"{URL}/entries/?event={camp.pk}").data["count"] == 1
    assert AuditLog.objects.filter(action="finance.entry_created").count() == 1

    # A closed event's finances are locked until it's reopened.
    camp.closed_at = timezone.now()
    camp.save()
    entry_id = res.data["id"]
    assert tr.get(f"{URL}/entries/{entry_id}/").data["locked"] is True
    assert tr.patch(f"{URL}/entries/{entry_id}/", {"description": "x"}, format="json").status_code == 400
    assert tr.delete(f"{URL}/entries/{entry_id}/").status_code == 400
    assert tr.post(f"{URL}/entries/", expense(setup, event=camp.pk), format="json").status_code == 400
    camp.closed_at = None
    camp.save()
    assert tr.patch(f"{URL}/entries/{entry_id}/", {"description": "Bread, Saturday"}, format="json").status_code == 200


def test_summary_and_filters(setup):
    tr, cats, camp, lbp = setup["tr"], setup["cats"], setup["camp"], setup["lbp"]
    today = timezone.localdate()
    last_year = today.replace(year=today.year - 1)
    tr.post(f"{URL}/entries/", expense(setup, date=str(today), amount="100", event=camp.pk), format="json")
    tr.post(f"{URL}/entries/", expense(setup, date=str(today), amount="895000", currency=lbp.pk,
            category=cats["Transport"].pk, description="Bus"), format="json")  # fmt: skip
    tr.post(f"{URL}/entries/", expense(setup, kind="income", date=str(today), amount="300",
            category=cats["Donations"].pk, description="Donation"), format="json")  # fmt: skip
    tr.post(f"{URL}/entries/", expense(setup, kind="income", date=str(last_year), amount="40",
            category=cats["Donations"].pk, description="Old donation"), format="json")  # fmt: skip

    data = tr.get(f"{URL}/summary/?range=year").data
    assert data["totals"] == {"income": 300.0, "expense": 110.0, "balance": 190.0, "count": 3}
    assert data["previous"]["income"] == 40.0
    assert data["events"] == {"expense": 100.0, "count": 1}
    by_cat = {c["name_en"]: c["total"] for c in data["by_category"]}
    assert by_cat == {"Donations": 300.0, "Food": 100.0, "Transport": 10.0}
    by_cur = {c["currency"]: c for c in data["by_currency"]}
    assert by_cur[lbp.pk]["net"] == -895000.0 and by_cur[lbp.pk]["net_default"] == -10.0
    assert by_cur[setup["usd"].pk]["net"] == 200.0
    assert data["default_currency"] == setup["usd"].pk

    all_time = tr.get(f"{URL}/summary/").data
    assert all_time["totals"]["count"] == 4 and all_time["period"]["start"] == last_year
    assert tr.get(f"{URL}/summary/?event={camp.pk}").data["totals"]["expense"] == 100.0

    def count(q):
        return tr.get(f"{URL}/entries/?{q}").data["count"]

    assert count("kind=income") == 2
    assert count(f"category={cats['Food'].pk},{cats['Transport'].pk}") == 2
    assert count(f"currency={lbp.pk}") == 1
    assert count("event=none") == 3 and count("event=any") == 1
    assert count("search=bus") == 1
    assert count("range=year") == 3
    start, end = last_year - timedelta(days=1), last_year + timedelta(days=1)
    assert count(f"range=custom&from={start}&to={end}") == 1
    assert tr.get(f"{URL}/entries/?range=decade").status_code == 400


def test_receipts(setup, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    tr = setup["tr"]
    data = expense(setup)
    data["receipt"] = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    res = tr.post(f"{URL}/entries/", data, format="multipart")
    assert res.status_code == 201, res.data
    assert res.data["receipt"]["name"] == "receipt.pdf" and res.data["receipt"]["url"].startswith("http")
    data = expense(setup)
    data["receipt"] = SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream")
    assert tr.post(f"{URL}/entries/", data, format="multipart").status_code == 400
    res = tr.patch(f"{URL}/entries/{res.data['id']}/", {"remove_receipt": True}, format="json")
    assert res.data["receipt"] is None
