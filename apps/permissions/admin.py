from django.contrib import admin

from .models import Permission, PermissionGrant, Role, RoleAssignment


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("codename", "group", "unit_scoped")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "is_system")
    list_filter = ("is_system",)
    filter_horizontal = ("permissions",)


admin.site.register(RoleAssignment)
admin.site.register(PermissionGrant)
