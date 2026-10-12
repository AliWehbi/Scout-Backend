from django.contrib import admin

from .models import AttendanceRecord, Session


class RecordInline(admin.TabularInline):
    model = AttendanceRecord
    extra = 0


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("title", "unit", "date", "organization")
    inlines = [RecordInline]
