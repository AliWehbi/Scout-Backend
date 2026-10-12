from django.contrib import admin

from .models import ConsentSignature, Event, EventInvitation, EventType


@admin.register(EventType)
class EventTypeAdmin(admin.ModelAdmin):
    list_display = ("name_en", "organization", "color", "default_mandatory", "is_archived")


class InvitationInline(admin.TabularInline):
    model = EventInvitation
    extra = 0
    raw_id_fields = ("member",)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "start_date", "event_type", "status", "organization")
    list_filter = ("status",)
    inlines = [InvitationInline]


@admin.register(ConsentSignature)
class ConsentSignatureAdmin(admin.ModelAdmin):
    list_display = ("signer_name", "invitation", "signed_at", "revoked_at")
