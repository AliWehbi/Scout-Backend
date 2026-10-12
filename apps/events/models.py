import secrets
from datetime import date, timedelta

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class TypeColor(models.TextChoices):
    """Event type colors are theme palette keys, so tenant branding recolors them too."""

    PRIMARY = "primary", _("Primary")
    SECONDARY = "secondary", _("Secondary")
    INFO = "info", _("Info")
    SUCCESS = "success", _("Success")
    WARNING = "warning", _("Warning")
    ERROR = "error", _("Error")


TYPE_ICONS = ("groups", "hiking", "camping", "sports_soccer", "volunteer_activism", "school", "celebration", "park")


class EventType(TenantModel):
    """A kind of event the organization runs. Admins manage these; Meeting and Activity are seeded."""

    name_en = models.CharField(max_length=80)
    name_ar = models.CharField(max_length=80, blank=True)
    color = models.CharField(max_length=10, choices=TypeColor.choices, default=TypeColor.PRIMARY)
    icon = models.CharField(max_length=40, default="groups")
    default_mandatory = models.BooleanField(default=False)
    default_requires_consent = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_en"], name="event_type_org_name_unique")]

    def __str__(self):
        return self.name_en


class EventStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    CANCELLED = "cancelled", _("Cancelled")


class Event(TenantModel):
    """
    A meeting or activity. With no units it is organization-wide. Families only see it once
    it is published and they are invited.
    """

    # RESTRICT: a type in use can't be deleted on its own, but deleting the organization removes both.
    event_type = models.ForeignKey(EventType, on_delete=models.RESTRICT, related_name="events")
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_date = models.DateField()
    end_time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    units = models.ManyToManyField("units.Unit", blank=True, related_name="events")
    leaders = models.ManyToManyField("members.Member", blank=True, related_name="led_events")
    is_mandatory = models.BooleanField(default=False)
    capacity = models.PositiveIntegerField(null=True, blank=True)
    reply_deadline = models.DateField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    requires_consent = models.BooleanField(default=False)
    consent_text = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=EventStatus.choices, default=EventStatus.DRAFT)
    # Events created together with "Repeat weekly" share a series id.
    series = models.UUIDField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["start_date", "start_time", "id"]
        indexes = [models.Index(fields=["organization", "start_date"])]

    def __str__(self):
        return f"{self.title} ({self.start_date})"

    @property
    def days(self) -> list[date]:
        """Every calendar day the event covers; attendance is taken once per day."""
        span = (self.end_date - self.start_date).days
        return [self.start_date + timedelta(days=i) for i in range(span + 1)]

    @property
    def replies_open(self) -> bool:
        if self.status != EventStatus.PUBLISHED:
            return False
        return self.reply_deadline is None or date.today() <= self.reply_deadline


class Response(models.TextChoices):
    GOING = "going", _("Going")
    MAYBE = "maybe", _("Maybe")
    NOT_GOING = "not_going", _("Not going")


class RespondedAs(models.TextChoices):
    SELF = "self", _("Member")
    PARENT = "parent", _("Parent")
    LEADER = "leader", _("Leader")


class EventInvitation(models.Model):
    """A member invited to an event, with their reply. Parents see it when `include_parents` is set."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invitations")
    member = models.ForeignKey("members.Member", on_delete=models.CASCADE, related_name="event_invitations")
    include_parents = models.BooleanField(default=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    invited_at = models.DateTimeField(auto_now_add=True)
    response = models.CharField(max_length=10, choices=Response.choices, blank=True)
    waitlisted = models.BooleanField(default=False)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    responded_as = models.CharField(max_length=10, choices=RespondedAs.choices, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "member"], name="event_invitation_unique")]
        ordering = ["member__first_name", "member__last_name"]

    @property
    def valid_signature(self):
        return next((s for s in self.signatures.all() if s.revoked_at is None), None)


class ConsentSignature(models.Model):
    """
    A parent's signature. Stores the exact text agreed to. When a leader edits the text and
    asks parents to sign again, existing signatures are revoked (kept for the record).
    """

    invitation = models.ForeignKey(EventInvitation, on_delete=models.CASCADE, related_name="signatures")
    guardian = models.ForeignKey("members.Guardian", on_delete=models.SET_NULL, null=True, related_name="+")
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    signer_name = models.CharField(max_length=200)
    text = models.TextField()
    signed_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-signed_at"]


def _feed_token() -> str:
    return secrets.token_urlsafe(24)


class CalendarFeed(models.Model):
    """Private iCal link for one login in one organization."""

    membership = models.OneToOneField(
        "organizations.OrgMembership", on_delete=models.CASCADE, related_name="calendar_feed"
    )
    token = models.CharField(max_length=64, unique=True, default=_feed_token)
    created_at = models.DateTimeField(auto_now_add=True)
