"""A user of organization A must never read or write organization B's data."""

import pytest


@pytest.fixture
def two_orgs(world):
    a, b = world.org("Group A"), world.org("Group B")
    admin_a = world.member(a, world.user("admin.a"), owner=True)
    admin_b = world.member(b, world.user("admin.b"), owner=True)
    unit_b = world.unit(b, "B Cubs")
    world.unit(a, "A Cubs")
    return dict(a=a, b=b, admin_a=admin_a, admin_b=admin_b, unit_b=unit_b)


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/units/",
        "/api/v1/users/",
        "/api/v1/org/",
        "/api/v1/org/settings/",
        "/api/v1/roles/",
        "/api/v1/audit-logs/",
        "/api/v1/dashboard/summary/",
        "/api/v1/me/permissions/",
    ],
)
def test_header_for_foreign_org_is_forbidden(two_orgs, client_for, url):
    client = client_for(two_orgs["admin_a"].user, two_orgs["b"])
    assert client.get(url).status_code == 403


def test_lists_only_contain_own_org(two_orgs, client_for):
    client = client_for(two_orgs["admin_a"].user, two_orgs["a"])
    names = [u["name_en"] for u in client.get("/api/v1/units/").data["results"]]
    assert names == ["A Cubs"]
    usernames = [m["user"]["username"] for m in client.get("/api/v1/users/").data["results"]]
    assert usernames == ["admin.a"]


def test_cannot_touch_foreign_objects_by_id(two_orgs, client_for):
    client = client_for(two_orgs["admin_a"].user, two_orgs["a"])
    assert client.get(f"/api/v1/units/{two_orgs['unit_b'].pk}/").status_code == 404
    assert client.patch(f"/api/v1/units/{two_orgs['unit_b'].pk}/", {"name_en": "x"}, format="json").status_code == 404
    assert (
        client.patch(f"/api/v1/users/{two_orgs['admin_b'].pk}/", {"first_name": "x"}, format="json").status_code == 404
    )


def test_single_membership_does_not_need_header(two_orgs, client_for):
    client = client_for(two_orgs["admin_a"].user)
    assert client.get("/api/v1/units/").status_code == 200


def test_multiple_memberships_require_header(world, two_orgs, client_for):
    user = two_orgs["admin_a"].user
    world.member(two_orgs["b"], user)
    assert client_for(user).get("/api/v1/units/").status_code == 403
    assert client_for(user, two_orgs["b"]).get("/api/v1/units/").status_code == 200
    orgs = client_for(user).get("/api/v1/me/organizations/").data
    assert {o["organization"]["name"] for o in orgs} == {"Group A", "Group B"}


def test_disabled_membership_loses_access(world, two_orgs, client_for):
    m = world.member(two_orgs["a"], world.user("gone"), status="disabled")
    assert client_for(m.user, two_orgs["a"]).get("/api/v1/units/").status_code == 403


def test_malformed_org_header(two_orgs, client_for):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(two_orgs["admin_a"].user)
    client.credentials(HTTP_X_ORGANIZATION_ID="not-a-uuid")
    assert client.get("/api/v1/units/").status_code == 403
