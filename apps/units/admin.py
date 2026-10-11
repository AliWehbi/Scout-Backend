from django.contrib import admin

from .models import Unit, UnitMembership


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("name_en", "name_ar", "organization", "is_active")
    search_fields = ("name_en", "name_ar")


admin.site.register(UnitMembership)
