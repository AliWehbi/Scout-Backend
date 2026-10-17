"""
Plain bookkeeping: money coming in (income) and going out (expenses).

Every entry has a category the admins create, and may belong to an event. Admins add the
currencies they use; one is the default and every total is shown in it. Each currency keeps
a current rate (how many of it make one of the default currency). A new entry starts from
that rate, can change it, and keeps its own copy, so changing a rate later never changes
past totals.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel

CENTS = Decimal("0.0001")


class Kind(models.TextChoices):
    INCOME = "income", _("Income")
    EXPENSE = "expense", _("Expense")


class Currency(TenantModel):
    code = models.CharField(max_length=3, help_text=_("ISO 4217 code, e.g. USD"))
    name_en = models.CharField(max_length=60)
    name_ar = models.CharField(max_length=60, blank=True)
    symbol_en = models.CharField(max_length=8)
    symbol_ar = models.CharField(max_length=8, blank=True)
    decimals = models.PositiveSmallIntegerField(default=2)
    # How many of this currency make 1 of the default currency (the default itself is 1).
    rate = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    rate_updated_at = models.DateTimeField(null=True, blank=True)
    rate_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_default = models.BooleanField(default=False)
    # Hidden currencies keep their entries but can't be picked for new ones.
    is_hidden = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-is_default", "order", "code"]
        verbose_name_plural = "currencies"
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="fin_currency_unique"),
            models.UniqueConstraint(fields=["organization"], condition=Q(is_default=True), name="fin_one_default"),
        ]

    def __str__(self):
        return self.code


class Category(TenantModel):
    kind = models.CharField(max_length=7, choices=Kind.choices)
    name_en = models.CharField(max_length=80)
    name_ar = models.CharField(max_length=80, blank=True)
    color = models.CharField(max_length=9, blank=True)
    order = models.PositiveIntegerField(default=0)
    # Hidden categories keep their entries but can't be picked for new ones.
    is_hidden = models.BooleanField(default=False)

    class Meta:
        ordering = ["kind", "order", "name_en"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(fields=["organization", "kind", "name_en"], name="fin_category_unique")]

    def __str__(self):
        return self.name_en


class Entry(TenantModel):
    kind = models.CharField(max_length=7, choices=Kind.choices)
    date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    currency = models.ForeignKey(Currency, on_delete=models.RESTRICT, related_name="entries")
    # The rate used for this entry: how many of `currency` made 1 of the default currency.
    rate = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    # `amount` in the default currency, kept so totals don't depend on today's rates.
    amount_default = models.DecimalField(max_digits=18, decimal_places=4)
    category = models.ForeignKey(Category, on_delete=models.RESTRICT, related_name="entries")
    event = models.ForeignKey(
        "events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="finance_entries"
    )
    description = models.CharField(max_length=200)
    note = models.CharField(max_length=500, blank=True)
    receipt = models.FileField(upload_to="finance/receipts/", blank=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name_plural = "entries"
        indexes = [
            models.Index(fields=["organization", "date"]),
            models.Index(fields=["organization", "kind", "date"]),
        ]

    def save(self, *args, **kwargs):
        self.amount_default = (Decimal(self.amount) / Decimal(self.rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} ({self.amount} {self.currency.code})"
