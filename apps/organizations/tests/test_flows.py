import pytest
from django.contrib.auth import get_user_model

from apps.members.models import Member
from apps.organizations.models import Invitation, OrgMembership
from apps.permissions.models import Role
from apps.units.models import Unit
from conftest import PASSWORD

SIGNUP = {
    "organization": {
        "name": "1st Cedar Scout Group",
        "country": "LB",
        "currency": "USD",
        "timezone": "Asia/Beirut",
        "default_language": "ar",
    },
    "admin": {"first_name": "Rania", "last_name": "Khoury", "username": "rania", "email": "", "password": PASSWORD},
    "units": [{"name_en": "Cubs", "name_ar": "الأشبال"}, {"name_en": "Scouts"}],
    "branding": {"primary_color": "#1e3d6b", "secondary_color": ""},
}


def test_signup_creates_org_owner_roles_and_units(db, anon):
    res = anon.post("/api/v1/auth/signup/", SIGNUP, format="json")
    assert res.status_code == 201, res.data
    assert res.data["access"]
    m = res.data["membership"]
    assert m["is_owner"] and m["roles"] == [{"key": "org_admin", "name": "Org Admin"}]
    assert m["theme"]["primary_color"] == "#1E3D6B"
    assert res.data["user"]["email"] is None
    assert res.data["user"]["preferred_language"] == "ar"
    membership = OrgMembership.objects.get(pk=m["id"])
    assert Unit.objects.filter(organization=membership.organization).count() == 2
    assert Role.objects.filter(organization=membership.organization, is_system=True).count() == 7
    # the owner is a login only; they add themselves on the Members page if they're a member
    assert not Member.objects.filter(organization=membership.organization).exists()


def test_signup_rejects_duplicate_username(world, anon):
    world.user("rania")
    res = anon.post("/api/v1/auth/signup/", SIGNUP, format="json")
    assert res.status_code == 400
    assert "admin" in res.data["errors"]


@pytest.fixture
def org_setup(world):
    org = world.org()
    cubs = world.unit(org, "Cubs")
    admin = world.member(org, world.user("rania"), owner=True)
    return dict(org=org, cubs=cubs, admin=admin, client=None)


def test_add_member_with_password_and_sign_in(org_setup, client_for, anon):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    roles = client.get("/api/v1/roles/").data["results"]
    scout_role = next(r for r in roles if r["key"] == "scout")
    res = client.post(
        "/api/v1/users/",
        {
            "first_name": "Omar",
            "username": "omar.t",
            "password": "Hiking-Boots-42",
            "roles": [{"role": scout_role["id"], "unit": org_setup["cubs"].pk}],
        },
        format="json",
    )
    assert res.status_code == 201, res.data
    assert res.data["status"] == "active"
    assert res.data["user"]["has_email"] is False
    assert res.data["member_id"] is None
    assert (
        anon.post(
            "/api/v1/auth/login/", {"identifier": "omar.t", "password": "Hiking-Boots-42"}, format="json"
        ).status_code
        == 200
    )


def test_member_in_multiple_units(org_setup, world, client_for):
    scouts = world.unit(org_setup["org"], "Scouts")
    karim = Member.objects.create(organization=org_setup["org"], first_name="Karim", kind="adult")
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.post(
        "/api/v1/users/",
        {"first_name": "Karim", "username": "karim", "password": "Hiking-Boots-42", "member_id": karim.pk},
        format="json",
    )
    assert res.status_code == 201, res.data
    res = client.put(
        f"/api/v1/users/{res.data['id']}/units/",
        {
            "units": [
                {"unit": org_setup["cubs"].pk, "position": "leader"},
                {"unit": scouts.pk, "position": "assistant"},
            ]
        },
        format="json",
    )
    assert {u["position"] for u in res.data["units"]} == {"leader", "assistant"}
    leaders = client.get("/api/v1/users/?kind=leaders").data["results"]
    assert any(m["user"]["username"] == "karim" for m in leaders)
    in_scouts = client.get(f"/api/v1/users/?unit={scouts.pk}").data["results"]
    assert [m["user"]["username"] for m in in_scouts] == ["karim"]


def test_add_member_requires_password_or_invite(org_setup, client_for):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.post("/api/v1/users/", {"first_name": "Omar", "username": "omar"}, format="json")
    assert res.status_code == 400


def test_invite_without_email_then_accept_with_code(org_setup, client_for, anon):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.post("/api/v1/users/", {"first_name": "Lina", "last_name": "Saad", "invite": True}, format="json")
    assert res.status_code == 201, res.data
    assert res.data["status"] == "invited"
    assert res.data["user"]["username"] == "lina.saad"
    code = res.data["invitation"]["code"]

    preview = anon.get(f"/api/v1/auth/invitations/{code.lower()}/")
    assert preview.status_code == 200
    assert preview.data["organization"]["name"] == "1st Cedar"
    assert preview.data["first_name"] == "Lina"

    res = anon.post(
        f"/api/v1/auth/invitations/{code}/accept/",
        {"username": "lina.s", "password": "Forest-Path-2026", "preferred_language": "ar"},
        format="json",
    )
    assert res.status_code == 200, res.data
    assert res.data["user"]["preferred_language"] == "ar"
    assert res.data["membership"]["status"] == "active"
    assert (
        anon.post(
            "/api/v1/auth/login/", {"identifier": "lina.s", "password": "Forest-Path-2026"}, format="json"
        ).status_code
        == 200
    )
    # the code is single-use
    assert (
        anon.post(
            f"/api/v1/auth/invitations/{code}/accept/", {"username": "x", "password": "Forest-Path-2026"}, format="json"
        ).status_code
        == 400
    )


def test_existing_user_joins_with_invitation(org_setup, world, client_for):
    admin_client = client_for(org_setup["admin"].user, org_setup["org"])
    res = admin_client.post("/api/v1/users/", {"first_name": "Maya", "invite": True}, format="json")
    token = Invitation.objects.get(code=res.data["invitation"]["code"]).token

    other_org = world.org("Another group")
    maya = world.user("maya.existing")
    world.member(other_org, maya)
    res = client_for(maya).post(f"/api/v1/auth/invitations/{token}/accept/", {}, format="json")
    assert res.status_code == 200, res.data
    assert OrgMembership.objects.filter(user=maya).count() == 2
    assert not get_user_model().objects.filter(first_name="Maya").exclude(pk=maya.pk).exists()


def test_regenerate_invitation_revokes_old_code(org_setup, client_for, anon):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.post("/api/v1/users/", {"first_name": "Lina", "invite": True}, format="json")
    old_code = res.data["invitation"]["code"]
    new = client.post(f"/api/v1/users/{res.data['id']}/invitation/")
    assert new.status_code == 201 and new.data["code"] != old_code
    assert anon.get(f"/api/v1/auth/invitations/{old_code}/").data["is_usable"] is False


def test_admin_sets_password_for_member_without_email(org_setup, world, client_for, anon):
    m = world.member(org_setup["org"], world.user("omar.t"))
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.post(f"/api/v1/users/{m.pk}/set-password/", {"password": "New-Tent-Pole-9"}, format="json")
    assert res.status_code == 204
    assert (
        anon.post(
            "/api/v1/auth/login/", {"identifier": "omar.t", "password": "New-Tent-Pole-9"}, format="json"
        ).status_code
        == 200
    )


def test_cannot_disable_self_or_owner(org_setup, world, client_for):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    assert (
        client.patch(f"/api/v1/users/{org_setup['admin'].pk}/", {"status": "disabled"}, format="json").status_code
        == 400
    )
    assert client.delete(f"/api/v1/users/{org_setup['admin'].pk}/").status_code == 400


def test_disable_and_remove_member(org_setup, world, client_for):
    m = world.member(org_setup["org"], world.user("omar.t"))
    client = client_for(org_setup["admin"].user, org_setup["org"])
    res = client.patch(f"/api/v1/users/{m.pk}/", {"status": "disabled", "email": "omar@x.org"}, format="json")
    assert res.status_code == 200 and res.data["status"] == "disabled" and res.data["user"]["email"] == "omar@x.org"
    assert client.delete(f"/api/v1/users/{m.pk}/").status_code == 204


def test_counts(org_setup, world, client_for):
    world.member(org_setup["org"], world.user("p1"), roles=[("parent", None)])
    world.member(org_setup["org"], world.user("i1"), status="invited")
    data = client_for(org_setup["admin"].user, org_setup["org"]).get("/api/v1/users/counts/").data
    assert data == {"all": 3, "leaders": 0, "parents": 1, "scouts": 0, "invited": 1}


def test_theme_settings(org_setup, world, client_for):
    admin = client_for(org_setup["admin"].user, org_setup["org"])
    res = admin.patch(
        "/api/v1/org/settings/", {"primary_color": "#1e3d6b", "default_theme_mode": "dark"}, format="json"
    )
    assert res.status_code == 200 and res.data["primary_color"] == "#1E3D6B"
    assert admin.patch("/api/v1/org/settings/", {"primary_color": "navy"}, format="json").status_code == 400
    # reset to default
    assert admin.patch("/api/v1/org/settings/", {"primary_color": ""}, format="json").data["primary_color"] == ""

    member = world.member(org_setup["org"], world.user("scout1"))
    member_client = client_for(member.user, org_setup["org"])
    assert member_client.get("/api/v1/org/settings/").status_code == 200
    assert member_client.patch("/api/v1/org/settings/", {"primary_color": "#000000"}, format="json").status_code == 403


def test_dashboard_summary_and_audit(org_setup, client_for):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    client.post("/api/v1/units/", {"name_en": "Rovers", "name_ar": "الجوالة"}, format="json")
    data = client.get("/api/v1/dashboard/summary/").data
    assert data["counts"]["units"] == 2
    assert data["setup"]["units_added"] is True
    assert data["recent_activity"][0]["action"] == "unit.created"
    assert client.get("/api/v1/audit-logs/").data["results"][0]["action"] == "unit.created"


def test_units_validation(org_setup, client_for):
    client = client_for(org_setup["admin"].user, org_setup["org"])
    assert client.post("/api/v1/units/", {"name_en": "cubs"}, format="json").status_code == 400
    assert (
        client.post("/api/v1/units/", {"name_en": "X", "age_min": 12, "age_max": 8}, format="json").status_code == 400
    )


def test_audit_filter_by_target(org_setup, world, client_for):
    m = world.member(org_setup["org"], world.user("omar.t"))
    client = client_for(org_setup["admin"].user, org_setup["org"])
    client.post(f"/api/v1/users/{m.pk}/set-password/", {"password": "New-Tent-Pole-9"}, format="json")
    client.post("/api/v1/units/", {"name_en": "Rovers"}, format="json")
    res = client.get(f"/api/v1/audit-logs/?target_type=orgmembership&target_id={m.pk}")
    assert [e["action"] for e in res.data["results"]] == ["member.password_set"]
