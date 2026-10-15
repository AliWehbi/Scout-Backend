from django.test import Client

from apps.core.models import SiteSettings
from apps.organizations.models import Organization, OrgMembership
from apps.organizations.tests.test_flows import SIGNUP
from conftest import PASSWORD


def _disable_signup():
    settings = SiteSettings.load()
    settings.allow_public_signup = False
    settings.save()


def test_site_config_reports_signup_enabled_by_default(db, anon):
    res = anon.get("/api/v1/site/")
    assert res.status_code == 200
    assert res.data == {"signup_enabled": True}


def test_site_settings_is_a_singleton(db):
    SiteSettings.load()
    SiteSettings(allow_public_signup=False).save()
    assert SiteSettings.objects.count() == 1
    assert SiteSettings.load().allow_public_signup is False


def test_signup_rejected_when_disabled(db, anon):
    _disable_signup()
    assert anon.get("/api/v1/site/").data == {"signup_enabled": False}
    res = anon.post("/api/v1/auth/signup/", SIGNUP, format="json")
    assert res.status_code == 403
    assert res.data["code"] == "signup_disabled"
    assert not Organization.objects.exists()


def test_site_settings_admin_is_superuser_only(world):
    staff = world.user("staff", is_staff=True)
    client = Client()
    client.force_login(staff)
    assert client.get("/admin/core/sitesettings/").status_code == 403

    root = world.user("root", is_staff=True, is_superuser=True)
    client.force_login(root)
    res = client.get("/admin/core/sitesettings/")
    assert res.status_code == 302
    res = client.post("/admin/core/sitesettings/1/change/", {"allow_public_signup": ""})
    assert res.status_code == 302
    assert SiteSettings.load().allow_public_signup is False


def test_superuser_adds_organization_with_owner_in_admin(world):
    _disable_signup()
    root = world.user("root", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(root)
    res = client.post(
        "/admin/organizations/organization/add/",
        {
            "name": "2nd Cedar",
            "country": "LB",
            "currency": "USD",
            "timezone": "Asia/Beirut",
            "default_language": "en",
            "owner_first_name": "Rania",
            "owner_username": "Rania",
            "owner_email": "",
            "owner_password": PASSWORD,
        },
    )
    assert res.status_code == 302, res.context["adminform"].form.errors
    membership = OrgMembership.objects.select_related("organization", "user").get()
    assert membership.is_owner and membership.user.username == "rania"
    assert membership.organization.name == "2nd Cedar"
    assert membership.organization.settings is not None
    assert membership.role_assignments.filter(role__key="org_admin").exists()
