from django.contrib import admin

from .models import DocumentTemplate


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "organization", "is_default", "size", "created_at")
    list_filter = ("kind",)
    readonly_fields = [f.name for f in DocumentTemplate._meta.fields]
