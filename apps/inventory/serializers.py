from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.events.models import Event
from apps.members.models import Member
from apps.units.models import Unit

from .models import (
    BorrowerType,
    Category,
    Condition,
    ExternalBorrower,
    Item,
    ItemKind,
    ItemLog,
    Loan,
    LoanLine,
    Location,
    Piece,
    StockTakeLine,
)


def user_name(user) -> str:
    return (user.get_full_name() or user.username) if user else ""


class OrgScopedMixin:
    """Validates that related objects belong to the active organization."""

    def _check_org(self, obj, field):
        if obj is not None and obj.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError({field: [_("Unknown value.")]})
        return obj


class CategorySerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Category
        fields = ["id", "name_en", "name_ar", "order", "item_count"]
        read_only_fields = ["id", "order"]


class LocationSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Location
        fields = ["id", "name_en", "name_ar", "item_count"]
        read_only_fields = ["id"]


class ExternalBorrowerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalBorrower
        fields = ["id", "name", "contact_person", "phone", "email", "notes"]
        read_only_fields = ["id"]


class BriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name_en = serializers.CharField()
    name_ar = serializers.CharField()


class ItemSerializer(OrgScopedMixin, serializers.ModelSerializer):
    category = BriefSerializer(read_only=True)
    location = BriefSerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=Category.objects.all(), allow_null=True, required=False, write_only=True
    )
    location_id = serializers.PrimaryKeyRelatedField(
        source="location", queryset=Location.objects.all(), allow_null=True, required=False, write_only=True
    )
    status = serializers.SerializerMethodField()
    # TRACKED: how many pieces to create with a new item.
    pieces_count = serializers.IntegerField(write_only=True, required=False, min_value=0, max_value=200)

    class Meta:
        model = Item
        fields = [
            "id",
            "name_en",
            "name_ar",
            "kind",
            "category",
            "category_id",
            "location",
            "location_id",
            "shelf",
            "photo",
            "brand",
            "notes",
            "purchase_date",
            "unit_cost",
            "quantity",
            "damaged_quantity",
            "min_stock",
            "code_prefix",
            "is_archived",
            "status",
            "pieces_count",
            "created_at",
        ]
        read_only_fields = ["id", "damaged_quantity", "created_at"]

    def get_status(self, obj):
        return self.context.get("statuses", {}).get(obj.pk)

    def validate_category_id(self, value):
        return self._check_org(value, "category_id")

    def validate_location_id(self, value):
        return self._check_org(value, "location_id")

    def validate_code_prefix(self, value):
        return value.strip().upper()

    def validate(self, attrs):
        if self.instance is not None and "kind" in attrs and attrs["kind"] != self.instance.kind:
            raise serializers.ValidationError({"kind": [_("The kind can't be changed after creating the item.")]})
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if kind == ItemKind.TRACKED and "quantity" in attrs and self.instance is not None:
            attrs.pop("quantity")
        if self.instance is not None and kind == ItemKind.COUNTED and "quantity" in attrs:
            # Counted quantities change through stock actions so the history stays complete.
            attrs.pop("quantity")
        return attrs


class PieceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Piece
        fields = ["id", "number", "code", "condition", "condition_note"]
        read_only_fields = ["id", "number"]


class ConditionSerializer(serializers.Serializer):
    condition = serializers.ChoiceField(choices=Condition.choices)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    cost = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)


class StockActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["restock", "use", "add", "damaged", "repaired", "lost"])
    quantity = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    cost = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)


class AddPiecesSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=1, max_value=100)
    condition = serializers.ChoiceField(choices=[Condition.NEW, Condition.GOOD, Condition.FAIR], default=Condition.NEW)


class LogSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    piece_code = serializers.CharField(source="piece.code", default="", read_only=True)
    borrower = serializers.SerializerMethodField()

    class Meta:
        model = ItemLog
        fields = ["id", "kind", "piece", "piece_code", "loan", "borrower", "quantity", "cost", "note", "actor", "at"]

    def get_actor(self, obj):
        return user_name(obj.actor)

    def get_borrower(self, obj):
        loan = obj.loan
        return {"type": loan.borrower_type, "name": loan.borrower_name} if loan else None


# ---------------------------------------------------------------- loans


class LoanLineSerializer(serializers.ModelSerializer):
    item_name_en = serializers.CharField(source="item.name_en")
    item_name_ar = serializers.CharField(source="item.name_ar")
    kind = serializers.CharField(source="item.kind")
    piece_code = serializers.CharField(source="piece.code", default="")
    outstanding = serializers.IntegerField()
    checked_in_by = serializers.SerializerMethodField()

    class Meta:
        model = LoanLine
        fields = [
            "id",
            "item",
            "item_name_en",
            "item_name_ar",
            "kind",
            "piece",
            "piece_code",
            "quantity",
            "returned_quantity",
            "lost_quantity",
            "outstanding",
            "return_condition",
            "return_note",
            "returned_by_name",
            "returned_at",
            "checked_in_by",
            "checked_in_at",
        ]

    def get_checked_in_by(self, obj):
        return user_name(obj.checked_in_by)


def borrower_payload(loan: Loan) -> dict:
    if loan.borrower_type == BorrowerType.EVENT and loan.event:
        e = loan.event
        return {"id": e.pk, "name": e.title, "name_ar": "", "sub": str(e.start_date)}
    if loan.borrower_type == BorrowerType.UNIT and loan.unit:
        return {"id": loan.unit_id, "name": loan.unit.name_en, "name_ar": loan.unit.name_ar, "sub": ""}
    if loan.borrower_type == BorrowerType.MEMBER and loan.member:
        m = loan.member
        return {"id": m.pk, "name": m.full_name, "name_ar": m.name_ar, "sub": m.phone}
    if loan.borrower_type == BorrowerType.EXTERNAL and loan.external:
        x = loan.external
        sub = " · ".join(v for v in (x.contact_person, x.phone) if v)
        return {"id": x.pk, "name": x.name, "name_ar": "", "sub": sub}
    return {"id": None, "name": "", "name_ar": "", "sub": ""}


class LoanSerializer(serializers.ModelSerializer):
    borrower = serializers.SerializerMethodField()
    lines = LoanLineSerializer(many=True, read_only=True)
    checked_out_by = serializers.SerializerMethodField()
    overdue = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            "id",
            "borrower_type",
            "borrower",
            "due_date",
            "overdue",
            "note",
            "checked_out_by",
            "checked_out_at",
            "closed_at",
            "lines",
        ]

    def get_borrower(self, obj):
        return borrower_payload(obj)

    def get_checked_out_by(self, obj):
        return user_name(obj.checked_out_by)

    def get_overdue(self, obj):
        from django.utils import timezone

        if obj.closed_at or not obj.due_date:
            return False
        return obj.due_date < timezone.localdate() and any(line.outstanding for line in obj.lines.all())


class LineInSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    pieces = serializers.PrimaryKeyRelatedField(queryset=Piece.objects.all(), many=True, required=False, default=list)
    quantity = serializers.IntegerField(min_value=0, required=False, default=0)


class CheckOutSerializer(serializers.Serializer):
    borrower_type = serializers.ChoiceField(choices=BorrowerType.choices)
    event = serializers.PrimaryKeyRelatedField(queryset=Event.objects.all(), required=False, allow_null=True)
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), required=False, allow_null=True)
    member = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all(), required=False, allow_null=True)
    external = serializers.PrimaryKeyRelatedField(
        queryset=ExternalBorrower.objects.all(), required=False, allow_null=True
    )
    # Or describe a new external borrower inline.
    new_external = ExternalBorrowerSerializer(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    lines = LineInSerializer(many=True)


class ReturnLineSerializer(serializers.Serializer):
    line = serializers.PrimaryKeyRelatedField(queryset=LoanLine.objects.all())
    returned = serializers.IntegerField(min_value=0, default=0)
    lost = serializers.IntegerField(min_value=0, default=0)
    damaged = serializers.IntegerField(min_value=0, default=0)
    condition = serializers.ChoiceField(choices=Condition.choices, required=False, allow_blank=True, default="")
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class ReturnSerializer(serializers.Serializer):
    returns = ReturnLineSerializer(many=True)
    returned_by_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    returned_at = serializers.DateTimeField(required=False, allow_null=True)


class ReserveSerializer(serializers.Serializer):
    event = serializers.PrimaryKeyRelatedField(queryset=Event.objects.all())
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    quantity = serializers.IntegerField(min_value=0, default=0)
    pieces = serializers.PrimaryKeyRelatedField(queryset=Piece.objects.all(), many=True, required=False, default=list)


class StockTakeLineSerializer(serializers.ModelSerializer):
    item_name_en = serializers.CharField(source="item.name_en")
    item_name_ar = serializers.CharField(source="item.name_ar")
    kind = serializers.CharField(source="item.kind")
    piece_code = serializers.CharField(source="piece.code", default="")

    class Meta:
        model = StockTakeLine
        fields = ["id", "item", "item_name_en", "item_name_ar", "kind", "piece", "piece_code", "expected", "counted"]
        read_only_fields = ["id", "item", "piece", "expected"]
