import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Language
from apps.core.models import TimeStampedModel
from apps.core.validators import hex_color_validator

# Words that say nothing about which group it is, skipped when making the organization code.
CODE_STOP_WORDS = {
    "the",
    "of",
    "and",
    "for",
    "scout",
    "scouts",
    "scouting",
    "group",
    "groups",
    "troop",
    "association",
    "club",
    "team",
    "unit",
    "pack",
    "society",
    "organization",
    "organisation",
    "org",
    "boy",
    "girl",
}


def make_org_code(name: str, is_taken) -> str:
    """
    A short, readable, permanent code from the name: "1st Cedar Scout Group" -> "cedar" (then cedar2...).
    It starts every custom template code ({{ event.cedar_meeting_point }}), so it must never change.
    """
    words = [w for w in slugify(name, allow_unicode=False).split("-") if w]
    useful = [w for w in words if w not in CODE_STOP_WORDS and not w[0].isdigit()]
    base = "".join(ch for ch in (useful or words or [""])[0] if ch.isalnum())[:12]
    if not base or not base[0].isalpha():
        base = ("org" + base)[:12]
    code, n = base, 2
    while is_taken(code):
        code = f"{base}{n}"
        n += 1
    return code


class Organization(TimeStampedModel):
    """A tenant: one scout group / association using the dashboard."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    # Permanent prefix for this organization's custom template codes. Never edited.
    code = models.CharField(max_length=20, unique=True, editable=False)
    logo = models.ImageField(upload_to="org-logos/", blank=True, null=True)
    country = models.CharField(max_length=2, blank=True, help_text=_("ISO 3166-1 alpha-2 code"))
    currency = models.CharField(max_length=3, default="USD", help_text=_("ISO 4217 code"))
    timezone = models.CharField(max_length=64, default="UTC")
    default_language = models.CharField(max_length=5, choices=Language.choices, default=Language.ENGLISH)
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        if not self.code:
            self.code = make_org_code(self.name, lambda c: Organization.objects.filter(code=c).exists())
        super().save(*args, **kwargs)

    def _unique_slug(self) -> str:
        base = slugify(self.name, allow_unicode=False) or "org"
        slug, n = base, 2
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def __str__(self):
        return self.name


class ThemeMode(models.TextChoices):
    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")
    SYSTEM = "system", _("Match device")


class OrganizationSettings(TimeStampedModel):
    """
    Per-tenant global settings. Theme colors are optional overrides: any color left
    empty falls back to the defaults defined in the frontend's theme tokens.
    """

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="settings")
    primary_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    secondary_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    success_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    warning_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    error_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    info_color = models.CharField(max_length=7, blank=True, default="", validators=[hex_color_validator])
    default_theme_mode = models.CharField(max_length=10, choices=ThemeMode.choices, default=ThemeMode.LIGHT)

    THEME_COLOR_FIELDS = (
        "primary_color",
        "secondary_color",
        "success_color",
        "warning_color",
        "error_color",
        "info_color",
    )

    def save(self, *args, **kwargs):
        for field in self.THEME_COLOR_FIELDS:
            setattr(self, field, (getattr(self, field) or "").strip().upper())
        super().save(*args, **kwargs)

    def has_custom_colors(self) -> bool:
        return any(getattr(self, f) for f in self.THEME_COLOR_FIELDS)

    def __str__(self):
        return f"Settings for {self.organization}"


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    INVITED = "invited", _("Invited")
    DISABLED = "disabled", _("Disabled")


class OrgMembership(TimeStampedModel):
    """Links a user to an organization. Roles, unit memberships and grants hang off this."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    status = models.CharField(max_length=10, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE)
    is_owner = models.BooleanField(default=False, help_text=_("Owners always have every permission."))

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="membership_org_user_unique")]
        ordering = ["user__first_name", "user__last_name", "user__username"]

    @property
    def is_active(self) -> bool:
        return self.status == MembershipStatus.ACTIVE

    def __str__(self):
        # Used as the audit-log target label, so keep it human-friendly.
        return self.user.full_name


def _invitation_code() -> str:
    # Short enough to read out or type, long enough not to guess (8 chars of a 31-char alphabet).
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _invitation_token() -> str:
    return secrets.token_urlsafe(24)


def _invitation_expiry():
    return timezone.now() + timedelta(days=settings.INVITATION_TTL_DAYS)


class Invitation(TimeStampedModel):
    """
    One-time invitation that activates an `invited` membership.

    It can be shared as a link (token) or as a short code that is typed in on the
    sign-in page, so it works for people without an email address.
    """

    membership = models.ForeignKey(OrgMembership, on_delete=models.CASCADE, related_name="invitations")
    token = models.CharField(max_length=64, unique=True, default=_invitation_token)
    code = models.CharField(max_length=12, unique=True, default=_invitation_code)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    expires_at = models.DateTimeField(default=_invitation_expiry)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def organization(self) -> Organization:
        return self.membership.organization

    @property
    def is_usable(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > timezone.now()

    @property
    def url(self) -> str:
        return f"{settings.FRONTEND_URL}/invitation/{self.token}"

    def __str__(self):
        return f"Invitation {self.code} for {self.membership}"
