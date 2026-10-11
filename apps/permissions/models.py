from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


class Permission(models.Model):
    """A row per catalog entry (see catalog.py). Synced automatically after migrate."""

    codename = models.CharField(max_length=64, unique=True)
    group = models.CharField(max_length=32)
    unit_scoped = models.BooleanField(default=True)

    class Meta:
        ordering = ["group", "codename"]

    def __str__(self):
        return self.codename


class Role(TenantModel):
    """A named, editable bundle of permissions, defined per organization."""

    key = models.SlugField(max_length=40, blank=True, help_text=_("Set for the default roles; empty for custom roles."))
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(default=False, help_text=_("Default roles can be edited but not deleted."))
    permissions = models.ManyToManyField(Permission, blank=True, related_name="roles")

    class Meta:
        ordering = ["-is_system", "name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="role_org_name_unique")]

    def __str__(self):
        return self.name


class RoleAssignment(TimeStampedModel):
    """
    Gives a membership a role, either organization-wide (`unit` empty) or for one unit.
    For a unit-scoped assignment only the role's unit-scoped permissions apply.
    """

    membership = models.ForeignKey(
        "organizations.OrgMembership", on_delete=models.CASCADE, related_name="role_assignments"
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="assignments")
    unit = models.ForeignKey("units.Unit", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "role"], condition=Q(unit__isnull=True), name="role_assignment_org_unique"
            ),
            models.UniqueConstraint(
                fields=["membership", "role", "unit"],
                condition=Q(unit__isnull=False),
                name="role_assignment_unit_unique",
            ),
        ]


class PermissionGrant(TimeStampedModel):
    """An extra permission given directly to a membership, on top of its roles."""

    membership = models.ForeignKey(
        "organizations.OrgMembership", on_delete=models.CASCADE, related_name="permission_grants"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="grants")
    unit = models.ForeignKey("units.Unit", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "permission"], condition=Q(unit__isnull=True), name="grant_org_unique"
            ),
            models.UniqueConstraint(
                fields=["membership", "permission", "unit"], condition=Q(unit__isnull=False), name="grant_unit_unique"
            ),
        ]
