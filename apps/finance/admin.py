from django.contrib import admin

from .models import Category, Currency, Entry


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name_en", "organization", "rate", "is_default", "is_hidden")
    list_filter = ("organization",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name_en", "kind", "organization", "is_hidden")
    list_filter = ("organization", "kind")


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ("date", "kind", "description", "amount", "currency", "amount_default", "organization")
    list_filter = ("organization", "kind")
    raw_id_fields = ("event", "created_by", "updated_by")
