from datetime import timedelta

import pytest
from django.utils import timezone

from apps.permissions.models import Permission, PermissionGrant
from apps.permissions.service import PermissionService


@pytest.fixture
def setup(world):
    org = world.org()
    cubs, scouts, rovers = world.unit(org, "Cubs"), world.unit(org, "Scouts"), world.unit(org, "Rovers")
    admin = world.member(org, world.user("rania"), owner=True)
    karim = world.member(org, world.user("karim.h"), roles=[("unit_leader", cubs)])
    return dict(org=org, cubs=cubs, scouts=scouts, rovers=rovers, admin=admin, karim=karim)


def grant(membership, codename, unit=None, **extra):
    return PermissionGrant.objects.create(
        membership=membership, permission=Permission.objects.get(codename=codename), unit=unit, **extra
    )


def test_unit_role_applies_only_to_its_unit(setup):
    s = PermissionService(setup["karim"])
    assert s.has_perm("events.manage", setup["cubs"])
    assert not s.has_perm("events.manage", setup["scouts"])
    assert not s.has_perm("events.manage")  # not organization-wide
    assert s.unit_ids("events.manage") == {setup["cubs"].pk}


def test_unit_scoped_role_ignores_org_only_permissions(world, setup):
    m = world.member(setup["org"], world.user("x"), roles=[("org_admin", setup["cubs"])])
    s = PermissionService(m)
    assert s.has_perm("members.view", setup["cubs"])
    assert not s.has_perm_anywhere("settings.manage")


def test_grant_on_another_unit(setup):
    """Leader of unit A gets access to features of unit B, but not C."""
    grant(setup["karim"], "events.manage", setup["scouts"])
    s = PermissionService(setup["karim"])
    assert s.has_perm("events.manage", setup["scouts"])
    assert not s.has_perm("events.manage", setup["rovers"])


def test_org_wide_finance_grant(setup):
    grant(setup["karim"], "finance.view")
    s = PermissionService(setup["karim"])
    assert s.has_perm("finance.view")
    assert s.has_perm("finance.view", setup["rovers"])
    assert s.unit_ids("finance.view") is None


def test_expired_grant_is_ignored(setup):
    grant(setup["karim"], "finance.view", expires_at=timezone.now() - timedelta(minutes=1))
    grant(setup["karim"], "inventory.manage", expires_at=timezone.now() + timedelta(days=1))
    s = PermissionService(setup["karim"])
    assert not s.has_perm("finance.view")
    assert s.has_perm("inventory.manage")


def test_owner_has_everything(setup):
    s = PermissionService(setup["admin"])
    assert s.has_perm("permissions.manage") and s.has_perm("finance.manage_expenses", setup["rovers"])


def test_disabled_membership_has_nothing(setup):
    setup["karim"].status = "disabled"
    setup["karim"].save()
    assert not PermissionService(setup["karim"]).has_perm_anywhere("events.view")


# ------------------------------------------------------------- API


def test_my_permissions_endpoint(setup, client_for):
    grant(setup["karim"], "events.manage", setup["scouts"])
    res = client_for(setup["karim"].user, setup["org"]).get("/api/v1/me/permissions/")
    assert res.status_code == 200
    perms = {p["codename"]: p for p in res.data["permissions"]}
    assert perms["events.manage"]["org_wide"] is False
    assert perms["events.manage"]["unit_ids"] == sorted([setup["cubs"].pk, setup["scouts"].pk])
    assert "settings.manage" not in perms


def test_admin_grants_permissions_via_api(setup, client_for):
    client = client_for(setup["admin"].user, setup["org"])
    url = f"/api/v1/users/{setup['karim'].pk}/permissions/"
    payload = {
        "grants": [
            {"permission": "events.manage", "unit": setup["scouts"].pk},
            {"permission": "finance.view", "unit": None, "note": "Spring camp budget"},
        ]
    }
    res = client.put(url, payload, format="json")
    assert res.status_code == 200, res.data
    assert len(res.data["grants"]) == 2
    effective = {p["codename"]: p for p in res.data["effective"]}
    assert effective["finance.view"]["org_wide"]
    assert res.data["roles"][0]["role"]["key"] == "unit_leader"

    # Replacing with an empty list removes them
    res = client.put(url, {"grants": []}, format="json")
    assert res.status_code == 200 and res.data["grants"] == []


def test_org_only_permission_cannot_be_unit_scoped(setup, client_for):
    client = client_for(setup["admin"].user, setup["org"])
    res = client.put(
        f"/api/v1/users/{setup['karim'].pk}/permissions/",
        {"grants": [{"permission": "settings.manage", "unit": setup["cubs"].pk}]},
        format="json",
    )
    assert res.status_code == 400


def test_duplicate_grants_rejected(setup, client_for):
    client = client_for(setup["admin"].user, setup["org"])
    g = {"permission": "finance.view", "unit": None}
    res = client.put(f"/api/v1/users/{setup['karim'].pk}/permissions/", {"grants": [g, g]}, format="json")
    assert res.status_code == 400


def test_granting_requires_permissions_manage(setup, client_for):
    grant(setup["karim"], "users.view")
    client = client_for(setup["karim"].user, setup["org"])
    assert client.get(f"/api/v1/users/{setup['admin'].pk}/permissions/").status_code == 200
    res = client.put(
        f"/api/v1/users/{setup['karim'].pk}/permissions/", {"grants": [{"permission": "finance.view"}]}, format="json"
    )
    assert res.status_code == 403


def test_users_list_requires_users_view(setup, client_for):
    client = client_for(setup["karim"].user, setup["org"])
    assert client.get("/api/v1/users/").status_code == 403
    grant(setup["karim"], "users.view")
    assert client_for(setup["karim"].user, setup["org"]).get("/api/v1/users/").status_code == 200


def test_grant_unit_from_another_org_rejected(world, setup, client_for):
    other = world.org("Other group")
    foreign_unit = world.unit(other, "Foreign")
    client = client_for(setup["admin"].user, setup["org"])
    res = client.put(
        f"/api/v1/users/{setup['karim'].pk}/permissions/",
        {"grants": [{"permission": "events.view", "unit": foreign_unit.pk}]},
        format="json",
    )
    assert res.status_code == 400


def test_catalog(setup, client_for):
    res = client_for(setup["karim"].user).get("/api/v1/permissions/catalog/")
    assert res.status_code == 200
    groups = {g["key"]: g for g in res.data}
    assert {"members", "events", "finance", "inventory", "administration"} <= set(groups)
    settings_perm = next(p for p in groups["administration"]["permissions"] if p["codename"] == "settings.manage")
    assert settings_perm["unit_scoped"] is False


def test_roles_crud(setup, client_for):
    client = client_for(setup["admin"].user, setup["org"])
    res = client.post(
        "/api/v1/roles/", {"name": "Camp Chief", "permissions": ["events.view", "events.manage"]}, format="json"
    )
    assert res.status_code == 201, res.data
    role_id = res.data["id"]
    res = client.patch(f"/api/v1/roles/{role_id}/", {"permissions": ["events.view"]}, format="json")
    assert res.data["permissions"] == ["events.view"]
    assert client.post("/api/v1/roles/", {"name": "x", "permissions": ["nope.nope"]}, format="json").status_code == 400
    system_role = setup["karim"].role_assignments.first().role
    assert client.delete(f"/api/v1/roles/{system_role.pk}/").status_code == 400
    assert client.delete(f"/api/v1/roles/{role_id}/").status_code == 204


def test_assign_roles_api(setup, client_for):
    client = client_for(setup["admin"].user, setup["org"])
    roles = client.get("/api/v1/roles/").data["results"]
    treasurer = next(r for r in roles if r["key"] == "treasurer")
    leader = next(r for r in roles if r["key"] == "unit_leader")
    res = client.put(
        f"/api/v1/users/{setup['karim'].pk}/roles/",
        {
            "roles": [
                {"role": leader["id"], "unit": setup["cubs"].pk},
                {"role": leader["id"], "unit": setup["scouts"].pk},
                {"role": treasurer["id"], "unit": None},
            ]
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    assert len(res.data["roles"]) == 3
    s = PermissionService(setup["karim"])
    assert s.has_perm("events.manage", setup["scouts"]) and s.has_perm("finance.view")
