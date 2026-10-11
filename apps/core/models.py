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
