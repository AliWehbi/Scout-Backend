from datetime import date

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel
from apps.customfields.models import AppliesTo, CustomField, FieldSection, FieldType  # noqa: F401 (moved there)


class MemberStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    INACTIVE = "inactive", _("Inactive")
    ALUMNI = "alumni", _("Alumni")


class MemberKind(models.TextChoices):
    YOUTH = "youth", _("Youth member")
    ADULT = "adult", _("Adult")


class Gender(models.TextChoices):
    MALE = "male", _("Male")
    FEMALE = "female", _("Female")


def apply_status_dates(member, previous_status: str | None) -> None:
    """
    Keep `left_date` in step with the status: set when an active member becomes inactive or
    alumni (a date given explicitly is kept), cleared when they are active again.
    Inactive -> alumni keeps the original date; a member created as inactive gets none.
    """
    if member.status == MemberStatus.ACTIVE:
        member.left_date = None
    elif member.left_date is None and previous_status == MemberStatus.ACTIVE:
        member.left_date = date.today()


class Member(TenantModel):
    """
    A person in the organization. A login is optional: most young members have none,
    leaders do. Unit memberships belong to the Member; roles and permission grants stay
    on the login (OrgMembership).
    """

    membership = models.OneToOneField(
        "organizations.OrgMembership", on_delete=models.SET_NULL, null=True, blank=True, related_name="member"
    )
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150, blank=True)
    first_name_ar = models.CharField(max_length=150, blank=True)
    last_name_ar = models.CharField(max_length=150, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    photo = models.ImageField(upload_to="member-photos/", blank=True, null=True)
    kind = models.CharField(max_length=10, choices=MemberKind.choices, default=MemberKind.YOUTH)
    status = models.CharField(max_length=10, choices=MemberStatus.choices, default=MemberStatus.ACTIVE)
    join_date = models.DateField(null=True, blank=True)
    # Set when the member stops being active (inactive or alumni), cleared if they come back.
    left_date = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    custom_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["first_name", "last_name"]
        indexes = [models.Index(fields=["organization", "status"])]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.__dict__.get("status")
        return instance

    def save(self, *args, **kwargs):
        if self._state.adding and self.join_date is None:
            self.join_date = date.today()
        previous = None if self._state.adding else getattr(self, "_loaded_status", self.status)
        apply_status_dates(self, previous)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "status" in update_fields:
            kwargs["update_fields"] = {*update_fields, "left_date"}
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def name_ar(self) -> str:
        """Full name in Arabic, built from the two Arabic name parts."""
        return f"{self.first_name_ar} {self.last_name_ar}".strip()

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = date.today()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    def __str__(self):
        return self.full_name


class Guardian(TenantModel):
    """A parent or guardian. May have a login (parent account) or be a contact only."""

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    membership = models.OneToOneField(
        "organizations.OrgMembership", on_delete=models.SET_NULL, null=True, blank=True, related_name="guardian"
    )

    class Meta:
        ordering = ["first_name", "last_name"]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name


class Relationship(models.TextChoices):
    MOTHER = "mother", _("Mother")
    FATHER = "father", _("Father")
    GUARDIAN = "guardian", _("Guardian")
    GRANDPARENT = "grandparent", _("Grandparent")
    SIBLING = "sibling", _("Sibling")
    OTHER = "other", _("Other")


class GuardianLink(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="guardian_links")
    guardian = models.ForeignKey(Guardian, on_delete=models.CASCADE, related_name="links")
    relationship = models.CharField(max_length=20, choices=Relationship.choices, default=Relationship.GUARDIAN)
    is_primary = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["member", "guardian"], name="guardian_link_unique")]
        ordering = ["-is_primary", "id"]


class MedicalInfo(models.Model):
    """
    Sensitive data, only visible with `members.view_medical` for the member's unit.
    List fields are stored as JSON:
      allergies: [{name, severity (mild|moderate|severe), notes}]
      medications: [{name, dose, when}]
      emergency_contacts: [{name, relationship, phone}]
    """

    member = models.OneToOneField(Member, on_delete=models.CASCADE, related_name="medical")
    allergies = models.JSONField(default=list, blank=True)
    medications = models.JSONField(default=list, blank=True)
    emergency_contacts = models.JSONField(default=list, blank=True)
    conditions = models.TextField(blank=True)
    blood_type = models.CharField(max_length=5, blank=True)
    doctor_name = models.CharField(max_length=150, blank=True)
    doctor_phone = models.CharField(max_length=40, blank=True)
    insurance = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class Certification(TimeStampedModel):
    """A leader training or check (first aid, safeguarding...) with an optional expiry date."""

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="certifications")
    name = models.CharField(max_length=150)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["expires_on", "name"]


class RequestStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPROVED = "approved", _("Approved")
    PARTIAL = "partial", _("Partly approved")
    REJECTED = "rejected", _("Rejected")


class ChangeRequest(TenantModel):
    """Changes a parent asked for. Nothing is applied until a leader approves (all or some items)."""

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="change_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    guardian = models.ForeignKey(Guardian, on_delete=models.SET_NULL, null=True, blank=True, related_name="requests")
    message = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=RequestStatus.choices, default=RequestStatus.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Change request for {self.member}"


class ChangeRequestItem(models.Model):
    request = models.ForeignKey(ChangeRequest, on_delete=models.CASCADE, related_name="items")
    field = models.CharField(max_length=80)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    approved = models.BooleanField(null=True)

    class Meta:
        ordering = ["id"]
