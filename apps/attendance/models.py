from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Session(TenantModel):
    """A unit meeting (or outing) where attendance is taken."""

    unit = models.ForeignKey("units.Unit", on_delete=models.CASCADE, related_name="sessions")
    title = models.CharField(max_length=150)
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["-date", "-start_time", "-id"]

    def __str__(self):
        return f"{self.title} ({self.date})"


class AttendanceStatus(models.TextChoices):
    PRESENT = "present", _("Present")
    ABSENT = "absent", _("Absent")
    EXCUSED = "excused", _("Excused")


class AttendanceRecord(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="records")
    member = models.ForeignKey("members.Member", on_delete=models.CASCADE, related_name="attendance_records")
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "member"], name="attendance_record_unique")]
