from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantQuerySet(models.QuerySet):
    def for_org(self, organization):
        return self.filter(organization=organization)


class TenantModel(TimeStampedModel):
    """Base class for every record owned by an organization (tenant)."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="+",
    )

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True


class SiteSettings(models.Model):
    """Platform-wide settings (a single row), managed by superusers in the Django admin."""

    allow_public_signup = models.BooleanField(
        default=True,
        help_text="Let anyone create an organization from the sign-up page. "
        "When off, only superusers can add organizations (from this admin).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "site settings"
        verbose_name_plural = "site settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site settings"
