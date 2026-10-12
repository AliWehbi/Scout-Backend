from django.contrib import admin

from .models import (
    Certification,
    ChangeRequest,
    ChangeRequestItem,
    CustomField,
    Guardian,
    GuardianLink,
    MedicalInfo,
    Member,
)


class GuardianLinkInline(admin.TabularInline):
    model = GuardianLink
    extra = 0


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "organization", "kind", "status", "membership")
    list_filter = ("kind", "status")
    search_fields = ("first_name", "last_name", "first_name_ar", "last_name_ar")
    inlines = [GuardianLinkInline]


@admin.register(Guardian)
class GuardianAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "phone", "organization", "membership")
    search_fields = ("first_name", "last_name", "phone")


class ChangeRequestItemInline(admin.TabularInline):
    model = ChangeRequestItem
    extra = 0


@admin.register(ChangeRequest)
class ChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("member", "guardian", "status", "created_at")
    list_filter = ("status",)
    inlines = [ChangeRequestItemInline]


admin.site.register(CustomField)
admin.site.register(MedicalInfo)
admin.site.register(Certification)
