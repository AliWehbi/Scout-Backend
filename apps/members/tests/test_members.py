import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.audit.models import AuditLog
from apps.members.models import ChangeRequest, CustomField, Guardian, GuardianLink, MedicalInfo, Member
from apps.permissions.models import Permission, PermissionGrant
from apps.units.models import UnitMembership


@pytest.fixture
def org(world):
    o = world.org()
    cubs, scouts = world.unit(o, "Cubs"), world.unit(o, "Scouts")
    admin = world.member(o, world.user("rania"), owner=True)
    karim = world.member(o, world.user("karim"), roles=[("unit_leader", cubs)])
    jad = Member.objects.create(organization=o, first_name="Jad", last_name="Saliba", kind="youth")
    UnitMembership.objects.create(member=jad, unit=cubs)
    omar = Member.objects.create(organization=o, first_name="Omar", last_name="Tannous", kind="youth")
    UnitMembership.objects.create(member=omar, unit=scouts)
    loner = Member.objects.create(organization=o, first_name="No", last_name="Unit", kind="adult")
    return dict(org=o, cubs=cubs, scouts=scouts, admin=admin, karim=karim, jad=jad, omar=omar, loner=loner)


def names(res):
    return sorted(m["first_name"] for m in res.data["results"])


def test_admin_sees_everyone(org, client_for):
    res = client_for(org["admin"].user, org["org"]).get("/api/v1/members/")
    assert res.status_code == 200
    assert names(res) == ["Jad", "No", "Omar"]


def test_unit_leader_sees_only_own_unit(org, client_for):
    client = client_for(org["karim"].user, org["org"])
    assert names(client.get("/api/v1/members/")) == ["Jad"]
    assert client.get(f"/api/v1/members/{org['omar'].pk}/").status_code == 404


def test_grant_on_another_unit_extends_visibility(org, client_for):
    PermissionGrant.objects.create(
        membership=org["karim"], permission=Permission.objects.get(codename="members.view"), unit=org["scouts"]
    )
    assert names(client_for(org["karim"].user, org["org"]).get("/api/v1/members/")) == ["Jad", "Omar"]


def test_member_without_permission_sees_nothing(org, world, client_for):
    scout = world.member(org["org"], world.user("s1"), roles=[("scout", org["cubs"])])
    assert client_for(scout.user, org["org"]).get("/api/v1/members/").data["count"] == 0


def test_leader_creates_member_only_in_own_unit(org, client_for):
    client = client_for(org["karim"].user, org["org"])
    ok = client.post("/api/v1/members/", {"first_name": "Lea", "units": [{"unit": org["cubs"].pk}]}, format="json")
    assert ok.status_code == 201, ok.data
    assert ok.data["units"][0]["unit"]["name_en"] == "Cubs"
    assert (
        client.post(
            "/api/v1/members/", {"first_name": "X", "units": [{"unit": org["scouts"].pk}]}, format="json"
        ).status_code
        == 403
    )
    assert client.post("/api/v1/members/", {"first_name": "X"}, format="json").status_code == 400


def test_tabs_counts_and_filters(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    UnitMembership.objects.create(member=org["loner"], unit=org["cubs"], position="leader")
    org["omar"].status = "alumni"
    org["omar"].save()
    counts = client.get("/api/v1/members/counts/").data
    assert counts["all"] == 3 and counts["youth"] == 1 and counts["leaders"] == 1 and counts["alumni"] == 1
    assert names(client.get("/api/v1/members/?tab=leaders")) == ["No"]
    assert names(client.get(f"/api/v1/members/?unit={org['cubs'].pk}&tab=youth")) == ["Jad"]
    assert names(client.get("/api/v1/members/?missing_info=1")) == ["Jad"]


def test_medical_requires_permission_and_is_audited(org, world, client_for):
    karim = client_for(org["karim"].user, org["org"])
    res = karim.put(
        f"/api/v1/members/{org['jad'].pk}/medical/",
        {
            "allergies": [{"name": "Peanuts", "severity": "severe"}],
            "emergency_contacts": [{"name": "Maya", "phone": "+961 3 123 456"}],
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    assert res.data["allergies"][0]["name"] == "Peanuts"
    res = karim.get(f"/api/v1/members/{org['jad'].pk}/medical/")
    assert res.data["emergency_contacts"][0]["name"] == "Maya"
    assert AuditLog.objects.filter(action="person.medical_viewed", target_id=str(org["jad"].pk)).exists()
    # assistant leader role has members.view but not view_medical
    helper = world.member(org["org"], world.user("helper"), roles=[("assistant_leader", org["cubs"])])
    assert client_for(helper.user, org["org"]).get(f"/api/v1/members/{org['jad'].pk}/medical/").status_code == 403
    # missing info flag cleared once there is an emergency contact (still missing DOB)
    detail = karim.get(f"/api/v1/members/{org['jad'].pk}/").data
    assert detail["flags"] == ["date_of_birth"]
    assert detail["access"]["view_medical"] is True


def test_medical_validation(org, client_for):
    res = client_for(org["admin"].user, org["org"]).put(
        f"/api/v1/members/{org['jad'].pk}/medical/", {"allergies": [{"name": "X", "severity": "deadly"}]}, format="json"
    )
    assert res.status_code == 400


def test_custom_fields_validation(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    res = client.post(
        "/api/v1/custom-fields/",
        {
            "label_en": "Uniform size",
            "label_ar": "مقاس الزي",
            "field_type": "choice",
            "options": ["S", "M"],
            "applies_to": "youth",
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    assert res.data["key"] == "uniform_size"
    assert (
        client.post(
            "/api/v1/custom-fields/", {"label_en": "X", "field_type": "choice", "options": []}, format="json"
        ).status_code
        == 400
    )
    jad = f"/api/v1/members/{org['jad'].pk}/"
    assert client.patch(jad, {"custom_data": {"uniform_size": "XXL"}}, format="json").status_code == 400
    assert client.patch(jad, {"custom_data": {"nope": 1}}, format="json").status_code == 400
    assert client.patch(jad, {"custom_data": {"uniform_size": "M"}}, format="json").data["custom_data"] == {
        "uniform_size": "M"
    }
    # adults don't get youth-only fields
    assert (
        client.patch(
            f"/api/v1/members/{org['loner'].pk}/", {"custom_data": {"uniform_size": "M"}}, format="json"
        ).status_code
        == 400
    )
    # non-admins can read fields but not change them
    karim = client_for(org["karim"].user, org["org"])
    assert karim.get("/api/v1/custom-fields/").status_code == 200
    assert karim.post("/api/v1/custom-fields/", {"label_en": "Y"}, format="json").status_code == 403


def test_guardians_replace(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    res = client.put(
        f"/api/v1/members/{org['jad'].pk}/guardians/",
        {
            "guardians": [
                {
                    "first_name": "Maya",
                    "last_name": "Saliba",
                    "phone": "+961 3 123 456",
                    "relationship": "mother",
                    "is_primary": True,
                }
            ]
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    link = res.data["guardian_links"][0]
    assert link["guardian"]["full_name"] == "Maya Saliba" and link["is_primary"]
    # re-linking the same guardian by id keeps one guardian record
    gid = link["guardian"]["id"]
    client.put(
        f"/api/v1/members/{org['jad'].pk}/guardians/",
        {"guardians": [{"guardian_id": gid, "relationship": "mother"}]},
        format="json",
    )
    assert Guardian.objects.count() == 1


@pytest.fixture
def family(org, world):
    parent_login = world.member(org["org"], world.user("maya", email="maya@x.org"), roles=[("parent", None)])
    guardian = Guardian.objects.create(organization=org["org"], first_name="Maya", membership=parent_login)
    GuardianLink.objects.create(member=org["jad"], guardian=guardian, relationship="mother", is_primary=True)
    CustomField.objects.create(
        organization=org["org"],
        key="uniform_size",
        label_en="Uniform",
        field_type="choice",
        options=["S", "M"],
        editable_by_parents=True,
    )
    CustomField.objects.create(
        organization=org["org"], key="scarf", label_en="Scarf", field_type="date", editable_by_parents=False
    )
    MedicalInfo.objects.create(member=org["jad"], allergies=[{"name": "Peanuts", "severity": "severe", "notes": ""}])
    return dict(login=parent_login, guardian=guardian)


def test_parent_sees_only_own_children(org, family, client_for):
    client = client_for(family["login"].user, org["org"])
    assert client.get("/api/v1/my/family/").data == {
        "is_guardian": True,
        "children": 1,
        "pending_requests": 0,
        "has_events": True,
        "events_pending": 0,
        "gear_out": 0,
    }
    kids = client.get("/api/v1/my/family/children/").data
    assert [k["first_name"] for k in kids] == ["Jad"]
    assert "custom.uniform_size" in kids[0]["editable_fields"] and "custom.scarf" not in kids[0]["editable_fields"]
    assert client.get(f"/api/v1/my/family/children/{org['omar'].pk}/").status_code == 403
    # parents don't get the leaders' members list
    assert client.get("/api/v1/members/").data["count"] == 0


def test_change_request_flow_with_partial_approval(org, family, client_for):
    parent = client_for(family["login"].user, org["org"])
    url = f"/api/v1/my/family/children/{org['jad'].pk}/"
    assert parent.post(url, {"changes": {"custom.scarf": "2026-01-01"}}, format="json").status_code == 400
    assert parent.post(url, {"changes": {"first_name": "Hacked"}}, format="json").status_code == 400
    assert parent.post(url, {"changes": {"custom.uniform_size": "XXL"}}, format="json").status_code == 400
    res = parent.post(
        url,
        {
            "message": "New allergy",
            "changes": {
                "medical.allergies": [
                    {"name": "Peanuts", "severity": "severe"},
                    {"name": "Sesame", "severity": "severe"},
                ],
                "custom.uniform_size": "M",
                "phone": "+961 3 999 999",
            },
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    assert len(res.data["items"]) == 3
    # nothing applied yet
    org["jad"].refresh_from_db()
    assert org["jad"].phone == "" and org["jad"].custom_data == {}

    leader = client_for(org["karim"].user, org["org"])
    pending = leader.get("/api/v1/change-requests/?status=pending").data["results"]
    assert len(pending) == 1
    items = {i["field"]: i["id"] for i in pending[0]["items"]}
    res = leader.post(
        f"/api/v1/change-requests/{pending[0]['id']}/approve/",
        {"approve_items": [items["medical.allergies"], items["custom.uniform_size"]], "note": "Thanks!"},
        format="json",
    )
    assert res.status_code == 200, res.data
    assert res.data["status"] == "partial"
    org["jad"].refresh_from_db()
    assert org["jad"].custom_data == {"uniform_size": "M"} and org["jad"].phone == ""
    assert [a["name"] for a in org["jad"].medical.allergies] == ["Peanuts", "Sesame"]
    mine = parent.get("/api/v1/my/family/requests/").data
    assert mine[0]["status"] == "partial" and mine[0]["review_note"] == "Thanks!"
    # can't review twice
    assert leader.post(f"/api/v1/change-requests/{pending[0]['id']}/reject/", {}, format="json").status_code == 400


def test_leader_of_other_unit_cannot_review(org, family, world, client_for):
    parent = client_for(family["login"].user, org["org"])
    parent.post(f"/api/v1/my/family/children/{org['jad'].pk}/", {"changes": {"phone": "+961 1 1"}}, format="json")
    scouts_leader = world.member(org["org"], world.user("sl"), roles=[("unit_leader", org["scouts"])])
    client = client_for(scouts_leader.user, org["org"])
    assert client.get("/api/v1/change-requests/").data["count"] == 0
    request_id = ChangeRequest.objects.get().pk
    assert client.post(f"/api/v1/change-requests/{request_id}/approve/", {}, format="json").status_code == 404


def test_medical_values_hidden_without_permission(org, family, world, client_for):
    parent = client_for(family["login"].user, org["org"])
    parent.post(
        f"/api/v1/my/family/children/{org['jad'].pk}/", {"changes": {"medical.blood_type": "A+"}}, format="json"
    )
    editor = world.member(org["org"], world.user("ed"))
    PermissionGrant.objects.create(membership=editor, permission=Permission.objects.get(codename="members.edit"))
    client = client_for(editor.user, org["org"])
    item = client.get("/api/v1/change-requests/").data["results"][0]["items"][0]
    assert item["restricted"] is True and item["new_value"] is None
    request_id = ChangeRequest.objects.get().pk
    assert client.post(f"/api/v1/change-requests/{request_id}/approve/", {}, format="json").status_code == 403


def test_give_access(org, client_for, anon):
    client = client_for(org["admin"].user, org["org"])
    res = client.post(f"/api/v1/members/{org['omar'].pk}/give-access/", {"invite": True}, format="json")
    assert res.status_code == 201, res.data
    assert res.data["login"]["username"] == "omar.tannous" and res.data["invitation"]["code"]
    assert (
        client.post(f"/api/v1/members/{org['omar'].pk}/give-access/", {"invite": True}, format="json").status_code
        == 400
    )
    code = res.data["invitation"]["code"]
    assert (
        anon.post(
            f"/api/v1/auth/invitations/{code}/accept/",
            {"username": "omar", "password": "Pine-Cone-2026"},
            format="json",
        ).status_code
        == 200
    )
    org["omar"].refresh_from_db()
    assert org["omar"].membership.user.username == "omar"
    # karim (not users.manage) can't
    assert (
        client_for(org["karim"].user, org["org"])
        .post(f"/api/v1/members/{org['jad'].pk}/give-access/", {}, format="json")
        .status_code
        == 403
    )


def test_users_api_units_go_through_member(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    res = client.post(
        "/api/v1/users/",
        {
            "first_name": "Lina",
            "username": "lina",
            "password": "Hiking-Boots-42",
            "units": [{"unit": org["cubs"].pk, "position": "leader"}],
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    member = Member.objects.get(pk=res.data["member_id"])
    assert member.kind == "adult" and member.unit_memberships.get().position == "leader"
    assert res.data["units"][0]["unit"]["name_en"] == "Cubs"


def test_parent_only_user_becomes_guardian(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    roles = client.get("/api/v1/roles/").data["results"]
    parent_role = next(r for r in roles if r["key"] == "parent")
    res = client.post(
        "/api/v1/users/",
        {
            "first_name": "Rima",
            "username": "rima",
            "password": "Hiking-Boots-42",
            "roles": [{"role": parent_role["id"], "unit": None}],
        },
        format="json",
    )
    assert res.status_code == 201 and res.data["member_id"] is None
    assert Guardian.objects.filter(membership_id=res.data["id"]).exists()


CSV = """First Name,Surname,DOB,Section,Parent,Parent mobile,Allergies,T-shirt,Notes
Elie,Mansour,02/06/2017,Cubs,Rita Mansour,+961 3 220 118,Pollen,S,x
Jad,Saliba,,Cubs,Maya Saliba,+961 3 123 456,,,
Maria,Khalil,31/02/2016,Cubs,Sami Khalil,+961 71 404 002,,,
Yasmine,Rizk,07/08/2018,Beavers,Fouad Rizk,+961 3 777 210,,,
,Nassar,12/12/2016,Cubs,Hiba Nassar,+961 70 300 441,,,
"""


def upload(text=CSV, name="cubs.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def test_import_preview_and_commit(org, client_for):
    CustomField.objects.create(
        organization=org["org"], key="uniform_size", label_en="Uniform size", field_type="choice", options=["S", "M"]
    )
    client = client_for(org["admin"].user, org["org"])
    preview = client.post("/api/v1/members/import/preview/", {"file": upload()}, format="multipart")
    assert preview.status_code == 200, preview.data
    data = preview.data
    assert data["mapping"]["First Name"] == "first_name" and data["mapping"]["Section"] == "unit"
    assert data["mapping"]["Notes"] == ""
    assert data["summary"] == {"total": 5, "ready": 1, "errors": 3, "duplicates": 1}
    statuses = {r["row"]: r["status"] for r in data["rows"]}
    assert statuses == {2: "ready", 3: "duplicate", 4: "error", 5: "error", 6: "error"}
    assert "unit" in data["rows"][3]["errors"]
    mapping = dict(data["mapping"], **{"T-shirt": "custom.uniform_size"})
    import json

    res = client.post(
        "/api/v1/members/import/commit/",
        {"file": upload(), "mapping": json.dumps(mapping), "decisions": json.dumps({"3": "update"})},
        format="multipart",
    )
    assert res.status_code == 200, res.data
    assert res.data == {"created": 1, "updated": 1, "skipped": 3}
    elie = Member.objects.get(first_name="Elie")
    assert elie.unit_memberships.get().unit == org["cubs"]
    assert elie.custom_data == {"uniform_size": "S"}
    assert elie.guardian_links.get().guardian.phone == "+961 3 220 118"
    assert elie.medical.allergies[0]["name"] == "Pollen"


def test_import_requires_org_wide_edit(org, client_for):
    res = client_for(org["karim"].user, org["org"]).post(
        "/api/v1/members/import/preview/", {"file": upload()}, format="multipart"
    )
    assert res.status_code == 403


def test_import_xlsx(org, client_for):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["First name", "Last name", "Unit"])
    ws.append(["Tala", "Aoun", "Scouts"])
    buffer = io.BytesIO()
    wb.save(buffer)
    file = SimpleUploadedFile("m.xlsx", buffer.getvalue())
    res = client_for(org["admin"].user, org["org"]).post(
        "/api/v1/members/import/preview/", {"file": file}, format="multipart"
    )
    assert res.status_code == 200 and res.data["summary"]["ready"] == 1


def test_bulk_and_export(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    res = client.post(
        "/api/v1/members/bulk/",
        {"ids": [org["jad"].pk, org["omar"].pk], "action": "set_status", "status": "inactive"},
        format="json",
    )
    assert res.status_code == 200 and res.data["updated"] == 2
    res = client.post(
        "/api/v1/members/bulk/",
        {"ids": [org["jad"].pk], "action": "add_to_unit", "unit": org["scouts"].pk},
        format="json",
    )
    assert org["jad"].unit_memberships.count() == 2
    karim = client_for(org["karim"].user, org["org"])
    assert (
        karim.post(
            "/api/v1/members/bulk/",
            {"ids": [org["jad"].pk], "action": "add_to_unit", "unit": org["scouts"].pk},
            format="json",
        ).status_code
        == 403
    )
    csv_text = client.get("/api/v1/members/export/").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("First name,Last name") and "Jad" in csv_text


def test_certifications_and_dashboard(org, client_for):
    from datetime import date, timedelta

    client = client_for(org["admin"].user, org["org"])
    res = client.post(
        f"/api/v1/members/{org['loner'].pk}/certifications/",
        {"name": "First aid", "expires_on": str(date.today() + timedelta(days=10))},
        format="json",
    )
    assert res.status_code == 201
    dash = client.get("/api/v1/dashboard/summary/").data
    assert dash["expiring_certifications"][0]["name"] == "First aid"
    assert dash["counts"]["missing_info"] == 2
    cert_id = res.data["id"]
    assert client.delete(f"/api/v1/members/{org['loner'].pk}/certifications/{cert_id}/").status_code == 204


def test_arabic_name_parts(org, client_for):
    client = client_for(org["admin"].user, org["org"])
    res = client.post(
        "/api/v1/members/",
        {"first_name": "Lea", "first_name_ar": "ليا", "last_name_ar": "حداد", "units": [{"unit": org["cubs"].pk}]},
        format="json",
    )
    assert res.status_code == 201, res.data
    detail = client.get(f"/api/v1/members/{res.data['id']}/").data
    assert (detail["first_name_ar"], detail["last_name_ar"], detail["name_ar"]) == ("ليا", "حداد", "ليا حداد")
    assert client.get("/api/v1/members/", {"search": "حداد"}).data["count"] == 1

    # A single full Arabic name column is split; separate columns are used as given.
    text = "First name,Arabic name,Last name (Arabic)\nElie,إيلي منصور,\nJana,جنى,خليل\n"
    import json

    preview = client.post("/api/v1/members/import/preview/", {"file": upload(text)}, format="multipart").data
    mapping = preview["mapping"]
    assert mapping == {"First name": "first_name", "Arabic name": "name_ar", "Last name (Arabic)": "last_name_ar"}
    client.post(
        "/api/v1/members/import/commit/",
        {"file": upload(text), "mapping": json.dumps(mapping), "decisions": "{}"},
        format="multipart",
    )
    elie, jana = Member.objects.get(first_name="Elie"), Member.objects.get(first_name="Jana")
    assert (elie.first_name_ar, elie.last_name_ar) == ("إيلي", "منصور")
    assert (jana.first_name_ar, jana.last_name_ar) == ("جنى", "خليل")


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_import_sample_round_trips(org, client_for, lang):
    CustomField.objects.create(
        organization=org["org"],
        key="uniform_size",
        label_en="Uniform size",
        label_ar="مقاس الزي",
        field_type="choice",
        options=["S", "M"],
    )
    client = client_for(org["admin"].user, org["org"])
    res = client.get("/api/v1/members/import/sample/", HTTP_ACCEPT_LANGUAGE=lang)
    assert res.status_code == 200
    assert res["Content-Type"].startswith("application/vnd.openxmlformats")
    sample = SimpleUploadedFile("sample.xlsx", res.content)
    preview = client.post("/api/v1/members/import/preview/", {"file": sample}, format="multipart").data
    assert all(preview["mapping"].values()), preview["mapping"]
    assert "custom.uniform_size" in preview["mapping"].values()
    assert preview["summary"]["ready"] == 2, preview["rows"]
    import json

    sample.seek(0)
    client.post(
        "/api/v1/members/import/commit/",
        {"file": sample, "mapping": json.dumps(preview["mapping"]), "decisions": "{}"},
        format="multipart",
    )
    jana = Member.objects.get(first_name="Jana")
    assert (jana.first_name_ar, jana.gender, jana.custom_data) == ("جنى", "female", {"uniform_size": "S"})
    assert jana.unit_memberships.get().position == "patrol_leader"
    assert jana.guardian_links.get().relationship == "father"

    assert client_for(org["karim"].user, org["org"]).get("/api/v1/members/import/sample/").status_code == 403
