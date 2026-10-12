from django.contrib import admin

from .models import Category, ExternalBorrower, Item, Loan, LoanLine, Location, Piece


class PieceInline(admin.TabularInline):
    model = Piece
    extra = 0


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name_en", "kind", "quantity", "organization", "is_archived")
    list_filter = ("kind", "is_archived")
    inlines = [PieceInline]


class LineInline(admin.TabularInline):
    model = LoanLine
    extra = 0


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("id", "borrower_type", "checked_out_at", "due_date", "closed_at", "organization")
    inlines = [LineInline]


admin.site.register([Category, Location, ExternalBorrower])
