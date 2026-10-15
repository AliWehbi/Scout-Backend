from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel

from .storage import get_private_storage, template_path


class DocumentKind(models.TextChoices):
    EVENT_SHEET = "event_sheet", _("Event sheet")
    ATTENDANCE_SHEET = "attendance_sheet", _("Attendance sheet")
    CONSENT_SLIPS = "consent_slips", _("Consent slips")


class DocumentTemplate(TenantModel):
    """
    A Word template an organization uploaded for one kind of document. Placeholders use
    Jinja syntax (`{{ event.title }}`) and are filled in a sandbox, see `rendering.py`.
    """

    kind = models.CharField(max_length=20, choices=DocumentKind.choices)
    name = models.CharField(max_length=120)
    file = models.FileField(storage=get_private_storage, upload_to=template_path, max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    is_default = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["kind", "-is_default", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "kind"],
                condition=models.Q(is_default=True),
                name="document_template_one_default_per_kind",
            )
        ]

    def __str__(self):
        return self.name
