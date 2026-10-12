"""
The organization's shared equipment store.

Three kinds of item:
- TRACKED: each piece has its own code, condition and history (tents, stoves),
- COUNTED: identical pieces counted together (mess tins, rope),
- CONSUMABLE: used up, with a minimum stock (gas, plasters).

Equipment leaves the store through a Loan (to an event, unit, member or external borrower)
made of LoanLines. Every check-out and check-in records who did it and when.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Category(TenantModel):
    name_en = models.CharField(max_length=80)
    name_ar = models.CharField(max_length=80, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name_en"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_en"], name="inv_category_unique")]

    def __str__(self):
        return self.name_en


class Location(TenantModel):
    name_en = models.CharField(max_length=80)
    name_ar = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["name_en"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_en"], name="inv_location_unique")]

    def __str__(self):
        return self.name_en


class ItemKind(models.TextChoices):
    TRACKED = "tracked", _("One by one")
    COUNTED = "counted", _("By quantity")
    CONSUMABLE = "consumable", _("Consumable")


class Item(TenantModel):
    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120, blank=True)
    kind = models.CharField(max_length=12, choices=ItemKind.choices)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    shelf = models.CharField(max_length=80, blank=True, help_text=_("Shelf, box or spot inside the location"))
    photo = models.ImageField(upload_to="inventory/", blank=True, null=True)
    brand = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # COUNTED: how many we own; CONSUMABLE: how many are in stock. TRACKED uses pieces.
    quantity = models.PositiveIntegerField(default=0)
    # COUNTED only: how many of `quantity` are damaged.
    damaged_quantity = models.PositiveIntegerField(default=0)
    min_stock = models.PositiveIntegerField(null=True, blank=True)
    code_prefix = models.CharField(max_length=10, blank=True, help_text=_("TRACKED: pieces get codes like TNT-01"))
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["name_en"]

    def __str__(self):
        return self.name_en


class Condition(models.TextChoices):
    NEW = "new", _("New")
    GOOD = "good", _("Good")
    FAIR = "fair", _("Fair")
    DAMAGED = "damaged", _("Damaged")
    UNDER_REPAIR = "under_repair", _("Under repair")
    LOST = "lost", _("Lost")
    RETIRED = "retired", _("Retired")


# Pieces in these conditions are no longer part of the store.
GONE = (Condition.LOST, Condition.RETIRED)
# Pieces in these conditions can still be lent or booked, but with a warning.
NEEDS_CARE = (Condition.DAMAGED, Condition.UNDER_REPAIR)


class Piece(models.Model):
    """One physical piece of a TRACKED item."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="pieces")
    number = models.PositiveIntegerField()
    code = models.CharField(max_length=30)
    condition = models.CharField(max_length=14, choices=Condition.choices, default=Condition.GOOD)
    condition_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]
        constraints = [models.UniqueConstraint(fields=["item", "number"], name="inv_piece_number_unique")]

    def __str__(self):
        return f"{self.item.name_en} #{self.number}"


class ExternalBorrower(TenantModel):
    """A person or group outside the organization that borrows equipment. Kept for next time."""

    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BorrowerType(models.TextChoices):
    EVENT = "event", _("Event")
    UNIT = "unit", _("Unit")
    MEMBER = "member", _("Member")
    EXTERNAL = "external", _("External")


class Loan(TenantModel):
    borrower_type = models.CharField(max_length=10, choices=BorrowerType.choices)
    event = models.ForeignKey("events.Event", on_delete=models.RESTRICT, null=True, blank=True, related_name="loans")
    unit = models.ForeignKey("units.Unit", on_delete=models.RESTRICT, null=True, blank=True, related_name="loans")
    member = models.ForeignKey("members.Member", on_delete=models.RESTRICT, null=True, blank=True, related_name="loans")
    external = models.ForeignKey(
        ExternalBorrower, on_delete=models.RESTRICT, null=True, blank=True, related_name="loans"
    )
    due_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    checked_out_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    checked_out_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-checked_out_at", "-id"]
        indexes = [models.Index(fields=["organization", "closed_at"])]

    @property
    def borrower_name(self) -> str:
        if self.borrower_type == BorrowerType.EVENT and self.event:
            return self.event.title
        if self.borrower_type == BorrowerType.UNIT and self.unit:
            return self.unit.name_en
        if self.borrower_type == BorrowerType.MEMBER and self.member:
            return self.member.full_name
        if self.borrower_type == BorrowerType.EXTERNAL and self.external:
            return self.external.name
        return ""

    def __str__(self):
        return f"Loan to {self.borrower_name}"


class LoanLine(models.Model):
    """
    One piece (TRACKED) or a quantity (COUNTED / CONSUMABLE). Consumables are used up and not
    expected back, but unused ones can still be returned.
    """

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.RESTRICT, related_name="loan_lines")
    piece = models.ForeignKey(Piece, on_delete=models.RESTRICT, null=True, blank=True, related_name="loan_lines")
    quantity = models.PositiveIntegerField(default=1)
    returned_quantity = models.PositiveIntegerField(default=0)
    lost_quantity = models.PositiveIntegerField(default=0)
    return_condition = models.CharField(max_length=14, choices=Condition.choices, blank=True)
    return_note = models.CharField(max_length=255, blank=True)
    returned_by_name = models.CharField(max_length=150, blank=True, help_text=_("Who brought it back"))
    returned_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    checked_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    @property
    def is_consumable(self) -> bool:
        return self.item.kind == ItemKind.CONSUMABLE

    @property
    def outstanding(self) -> int:
        """How many are still out and expected back."""
        if self.is_consumable:
            return 0
        return self.quantity - self.returned_quantity - self.lost_quantity


class Reservation(TenantModel):
    """Equipment booked for an event's dates (the event's gear list)."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="reservations")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="reservations")
    piece = models.ForeignKey(Piece, on_delete=models.CASCADE, null=True, blank=True, related_name="reservations")
    quantity = models.PositiveIntegerField(default=1)
    loan_line = models.ForeignKey(LoanLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["item__name_en", "piece__number", "id"]


class LogKind(models.TextChoices):
    ADDED = "added", _("Added")
    UPDATED = "updated", _("Updated")
    CHECKED_OUT = "checked_out", _("Checked out")
    RETURNED = "returned", _("Returned")
    LOST = "lost", _("Lost")
    DAMAGED = "damaged", _("Damaged")
    REPAIR = "repair", _("Sent for repair")
    REPAIRED = "repaired", _("Repaired")
    RETIRED = "retired", _("Retired")
    RESTOCKED = "restocked", _("Restocked")
    ADJUSTED = "adjusted", _("Adjusted")


class ItemLog(models.Model):
    """History of an item (and its pieces). Every entry says who did it and when."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="logs")
    piece = models.ForeignKey(Piece, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs")
    loan = models.ForeignKey(Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs")
    kind = models.CharField(max_length=12, choices=LogKind.choices)
    quantity = models.IntegerField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    at = models.DateTimeField()

    class Meta:
        ordering = ["-at", "-id"]


class StockTake(TenantModel):
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    finished_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class StockTakeLine(models.Model):
    """Expected vs counted. TRACKED pieces are confirmed one by one (counted is 1 or 0)."""

    stocktake = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="+")
    piece = models.ForeignKey(Piece, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    expected = models.PositiveIntegerField()
    counted = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["item__name_en", "piece__number", "id"]
