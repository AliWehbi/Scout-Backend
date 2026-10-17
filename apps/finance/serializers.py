import os
import re
from decimal import Decimal

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.events import services as event_services
from apps.events.models import Event

from .models import CENTS, Category, Currency, Entry
from .services import RATE_PLACES

RECEIPT_MAX_BYTES = 10 * 1024 * 1024
RECEIPT_TYPES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic"}


def user_name(user) -> str:
    return (user.get_full_name() or user.username) if user else ""


class CurrencySerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, required=False)
    rate_updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Currency
        fields = [
            "id",
            "code",
            "name_en",
            "name_ar",
            "symbol_en",
            "symbol_ar",
            "decimals",
            "rate",
            "rate_updated_at",
            "rate_updated_by_name",
            "is_default",
            "is_hidden",
            "order",
            "entry_count",
        ]
        read_only_fields = ["id", "rate_updated_at", "is_default", "order"]
        extra_kwargs = {"decimals": {"min_value": 0, "max_value": 4}}

    def get_rate_updated_by_name(self, obj):
        return user_name(obj.rate_updated_by)

    def validate_code(self, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise serializers.ValidationError(_("Use the 3-letter currency code, e.g. USD."))
        org = self.context["organization"]
        clash = Currency.objects.filter(organization=org, code=value)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(_("This currency has already been added."))
        return value

    def validate_rate(self, value):
        if value <= 0:
            raise serializers.ValidationError(_("The rate must be more than zero."))
        return value

    def validate(self, attrs):
        instance = self.instance
        if instance and instance.is_default:
            attrs.pop("rate", None)  # the default currency is always 1
            if attrs.get("is_hidden"):
                raise serializers.ValidationError({"is_hidden": [_("The default currency can't be hidden.")]})
        if instance and "code" in attrs and attrs["code"] != instance.code and instance.entries.exists():
            raise serializers.ValidationError({"code": [_("The code can't change once entries use this currency.")]})
        return attrs


class CategorySerializer(serializers.ModelSerializer):
    entry_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Category
        fields = ["id", "kind", "name_en", "name_ar", "color", "order", "is_hidden", "entry_count"]
        read_only_fields = ["id", "order"]

    def validate_name_en(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(_("This field may not be blank."))
        return value

    def validate(self, attrs):
        org = self.context["organization"]
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if self.instance and "kind" in attrs and attrs["kind"] != self.instance.kind:
            raise serializers.ValidationError({"kind": [_("A category can't switch between income and expense.")]})
        name = attrs.get("name_en", getattr(self.instance, "name_en", ""))
        clash = Category.objects.filter(organization=org, kind=kind, name_en__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"name_en": [_("A category with this name already exists.")]})
        return attrs


class EntryValidationMixin:
    """Checks on the category, currency and event of an entry."""

    def _org(self):
        return self.context["organization"]

    def check_category(self, category, kind, current=None):
        if category.organization_id != self._org().pk:
            raise serializers.ValidationError({"category": [_("Unknown value.")]})
        if category.kind != kind:
            raise serializers.ValidationError(
                {"category": [_("Pick a category of the same type (income or expense).")]}
            )
        if category.is_hidden and category != current:
            raise serializers.ValidationError({"category": [_("This category is hidden. Pick another one.")]})

    def check_currency(self, currency, current=None):
        if currency.organization_id != self._org().pk:
            raise serializers.ValidationError({"currency": [_("Unknown value.")]})
        if currency.is_hidden and currency != current:
            raise serializers.ValidationError({"currency": [_("This currency is hidden. Pick another one.")]})

    def check_event(self, event):
        if event is None:
            return
        if event.organization_id != self._org().pk:
            raise serializers.ValidationError({"event": [_("Unknown value.")]})
        event_services.require_open(event)

    @staticmethod
    def resolve_rate(currency, rate):
        if currency.is_default:
            return Decimal(1)
        if rate is None:
            return currency.rate
        if rate <= 0:
            raise serializers.ValidationError({"rate": [_("The rate must be more than zero.")]})
        return rate


class EntrySerializer(EntryValidationMixin, serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all())
    currency = serializers.PrimaryKeyRelatedField(queryset=Currency.objects.all())
    event = serializers.PrimaryKeyRelatedField(queryset=Event.objects.all(), allow_null=True, required=False)
    rate = serializers.DecimalField(max_digits=20, decimal_places=8, required=False, allow_null=True)
    remove_receipt = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Entry
        fields = [
            "id",
            "kind",
            "date",
            "amount",
            "currency",
            "rate",
            "amount_default",
            "category",
            "event",
            "description",
            "note",
            "receipt",
            "remove_receipt",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "amount_default", "created_at", "updated_at"]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(_("Enter an amount more than zero."))
        return value

    def validate_receipt(self, value):
        if value is None:
            return value
        if value.size > RECEIPT_MAX_BYTES:
            raise serializers.ValidationError(_("The file is too large (10 MB at most)."))
        if os.path.splitext(value.name)[1].lower() not in RECEIPT_TYPES:
            raise serializers.ValidationError(_("Upload a PDF or a photo (JPG, PNG, WebP or HEIC)."))
        return value

    def validate(self, attrs):
        instance = self.instance
        if instance and instance.event_id:
            event_services.require_open(instance.event)  # a closed event's entries are locked
        kind = attrs.get("kind", getattr(instance, "kind", None))
        category = attrs.get("category", getattr(instance, "category", None))
        currency = attrs.get("currency", getattr(instance, "currency", None))
        if category is None or currency is None:
            return attrs
        self.check_category(category, kind, current=getattr(instance, "category", None))
        self.check_currency(currency, current=getattr(instance, "currency", None))
        if "event" in attrs:
            self.check_event(attrs["event"])
        if "rate" in attrs or "currency" in attrs or instance is None:
            keep = instance.rate if instance and currency == instance.currency else None
            attrs["rate"] = self.resolve_rate(currency, attrs.get("rate", keep))
        return attrs

    def save(self, **kwargs):
        remove = self.validated_data.pop("remove_receipt", False)
        if remove and self.instance and "receipt" not in self.validated_data:
            if self.instance.receipt:
                self.instance.receipt.delete(save=False)
            self.validated_data["receipt"] = None
        return super().save(**kwargs)

    def to_representation(self, obj):
        request = self.context.get("request")
        receipt = None
        if obj.receipt:
            url = obj.receipt.url
            receipt = {
                "url": request.build_absolute_uri(url) if request else url,
                "name": os.path.basename(obj.receipt.name),
            }
        currency, category, event = obj.currency, obj.category, obj.event
        return {
            "id": obj.pk,
            "kind": obj.kind,
            "date": obj.date.isoformat(),
            "amount": str(Decimal(obj.amount).quantize(CENTS)),
            "rate": str(Decimal(obj.rate).quantize(RATE_PLACES)),
            "amount_default": str(Decimal(obj.amount_default).quantize(CENTS)),
            "currency": {
                "id": currency.pk,
                "code": currency.code,
                "symbol_en": currency.symbol_en,
                "symbol_ar": currency.symbol_ar,
                "decimals": currency.decimals,
            },
            "category": {
                "id": category.pk,
                "kind": category.kind,
                "name_en": category.name_en,
                "name_ar": category.name_ar,
                "color": category.color,
            },
            "event": (
                {
                    "id": event.pk,
                    "title": event.title,
                    "start_date": event.start_date.isoformat(),
                    "closed": event.is_closed,
                }
                if event
                else None
            ),
            "description": obj.description,
            "note": obj.note,
            "receipt": receipt,
            "locked": bool(event and event.is_closed),
            "created_by_name": user_name(obj.created_by),
            "created_at": obj.created_at,
            "updated_by_name": user_name(obj.updated_by) if obj.updated_by_id != obj.created_by_id else "",
            "updated_at": obj.updated_at,
        }
