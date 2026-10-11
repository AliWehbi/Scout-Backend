from django.contrib import admin

from .models import Invitation, Organization, OrganizationSettings, OrgMembership


class SettingsInline(admin.StackedInline):
    model = OrganizationSettings


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "country", "default_language", "is_active", "created_at")
    search_fields = ("name", "slug")
    inlines = [SettingsInline]


@admin.register(OrgMembership)
class OrgMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "status", "is_owner")
    list_filter = ("status", "is_owner")
    search_fields = ("user__username", "user__email", "organization__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("code", "membership", "expires_at", "accepted_at", "revoked_at")
