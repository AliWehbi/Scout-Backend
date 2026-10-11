from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel
from apps.core.validators import hex_color_validator


class Unit(TenantModel):
    """A section of the organization, e.g. Cubs, Scouts, Rovers."""

    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120, blank=True)
    section = models.CharField(max_length=60, blank=True, help_text=_("Age section, e.g. Cubs"))
    age_min = models.PositiveSmallIntegerField(null=True, blank=True)
    age_max = models.PositiveSmallIntegerField(null=True, blank=True)
    color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["age_min", "name_en"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_en"], name="unit_org_name_unique")]

    def save(self, *args, **kwargs):
        self.color = (self.color or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name_en


class UnitPosition(models.TextChoices):
    LEADER = "leader", _("Leader")
    ASSISTANT = "assistant", _("Assistant leader")
    PATROL_LEADER = "patrol_leader", _("Patrol leader")
    MEMBER = "member", _("Member")


class UnitMembership(models.Model):
    """A person belongs to a unit with a position. One person can be in several units."""

    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name="memberships")
    membership = models.ForeignKey(
        "organizations.OrgMembership", on_delete=models.CASCADE, related_name="unit_memberships"
    )
    position = models.CharField(max_length=20, choices=UnitPosition.choices, default=UnitPosition.MEMBER)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["unit", "membership"], name="unit_membership_unique")]

    def __str__(self):
        return f"{self.membership.user} in {self.unit} ({self.position})"
