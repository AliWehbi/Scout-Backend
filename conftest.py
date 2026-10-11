import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.organizations.models import Organization, OrganizationSettings, OrgMembership
from apps.permissions.models import Role, RoleAssignment
from apps.permissions.service import create_default_roles
from apps.units.models import Unit


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()


@pytest.fixture(autouse=True)
def _fast_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


PASSWORD = "Trail-Blazer-2026"


class World:
    """Builds organizations, users and memberships for tests."""

    def org(self, name="1st Cedar"):
        org = Organization.objects.create(name=name)
        OrganizationSettings.objects.create(organization=org)
        self.roles = create_default_roles(org)
        return org

    def user(self, username, email=None, password=PASSWORD, **extra):
        return User.objects.create_user(username=username, email=email, password=password, **extra)

    def member(self, org, user, *, owner=False, roles=(), status="active"):
        membership = OrgMembership.objects.create(organization=org, user=user, is_owner=owner, status=status)
        for role_key, unit in roles:
            RoleAssignment.objects.create(
                membership=membership, role=Role.objects.get(organization=org, key=role_key), unit=unit
            )
        return membership

    def unit(self, org, name):
        return Unit.objects.create(organization=org, name_en=name)


@pytest.fixture
def world(db):
    return World()


@pytest.fixture
def client_for():
    """API client authenticated as `user`, sending `org` as the active organization."""

    def make(user, org=None):
        client = APIClient()
        client.force_authenticate(user)
        if org is not None:
            client.credentials(HTTP_X_ORGANIZATION_ID=str(org.pk))
        return client

    return make


@pytest.fixture
def anon():
    return APIClient()
