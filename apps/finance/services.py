from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.organizations.dashboard import resolve_period

from .models import Category, Currency, Entry, Kind
from .presets import preset

RATE_PLACES = Decimal("0.00000001")

# Starting categories for a new organization. Admins rename, hide or add to them in Settings.
DEFAULT_CATEGORIES = (
    (Kind.EXPENSE, "Transport", "النقل", "#2B6A95"),
    (Kind.EXPENSE, "Food", "الطعام", "#8F5D1C"),
    (Kind.EXPENSE, "Campsite & venue", "المخيم والمكان", "#2F5D3A"),
    (Kind.EXPENSE, "Equipment", "المعدّات", "#935700"),
    (Kind.EXPENSE, "Badges & uniforms", "الشارات واللباس", "#1D5E63"),
    (Kind.EXPENSE, "Hall & utilities", "القاعة والخدمات", "#595D53"),
    (Kind.INCOME, "Membership dues", "الاشتراكات", "#2F5D3A"),
    (Kind.INCOME, "Participation fees", "رسوم المشاركة", "#2E7D4F"),
    (Kind.INCOME, "Donations", "التبرعات", "#7A2335"),
)


def create_default_finance(organization) -> None:
    if not Currency.objects.filter(organization=organization).exists():
        # Start with the currency chosen for the organization (at sign-up or in General settings).
        Currency.objects.create(organization=organization, is_default=True, rate=1, **preset(organization.currency))
    if not Category.objects.filter(organization=organization).exists():
        Category.objects.bulk_create(
            Category(organization=organization, kind=kind, name_en=en, name_ar=ar, color=color, order=i)
            for i, (kind, en, ar, color) in enumerate(DEFAULT_CATEGORIES)
        )


def default_currency(organization) -> Currency:
    currency = Currency.objects.filter(organization=organization, is_default=True).first()
    if currency is None:
        create_default_finance(organization)
        currency = Currency.objects.get(organization=organization, is_default=True)
    return currency


@transaction.atomic
def make_default(currency: Currency, user) -> None:
    """
    Totals are kept in the default currency, so it can only change before any entry exists.
    The other rates are converted so they stay correct against the new default.
    """
    org = currency.organization
    if currency.is_default:
        return
    if Entry.objects.filter(organization=org).exists():
        raise ValidationError(
            {
                "detail": _(
                    "The default currency can't change once entries have been recorded, because every total is kept in it."
                )
            }
        )
    if currency.is_hidden:
        raise ValidationError({"detail": _("Show this currency again before making it the default.")})
    factor = currency.rate
    now = timezone.now()
    others = list(Currency.objects.select_for_update().filter(organization=org).exclude(pk=currency.pk))
    for other in others:
        other.is_default = False
        other.rate = (other.rate / factor).quantize(RATE_PLACES)
        other.rate_updated_at, other.rate_updated_by = now, user
    Currency.objects.bulk_update(others, ["is_default", "rate", "rate_updated_at", "rate_updated_by"])
    currency.is_default, currency.rate = True, Decimal(1)
    currency.rate_updated_at, currency.rate_updated_by = now, user
    currency.save(update_fields=["is_default", "rate", "rate_updated_at", "rate_updated_by"])
    # Keep the organization's currency (used for event costs) in step with the books.
    org.currency = currency.code
    org.save(update_fields=["currency"])


def _ids(value: str) -> list[int]:
    return [int(v) for v in (value or "").split(",") if v.strip().isdigit()]


def filter_entries(qs, params):
    """The filters shared by the entry list and the summary (everything except the period)."""
    if params.get("kind") in Kind.values:
        qs = qs.filter(kind=params["kind"])
    if ids := _ids(params.get("category", "")):
        qs = qs.filter(category_id__in=ids)
    if ids := _ids(params.get("currency", "")):
        qs = qs.filter(currency_id__in=ids)
    event = params.get("event", "")
    if event == "none":
        qs = qs.filter(event__isnull=True)
    elif event == "any":
        qs = qs.filter(event__isnull=False)
    elif ids := _ids(event):
        qs = qs.filter(event_id__in=ids)
    if q := params.get("search", "").strip():
        qs = qs.filter(Q(description__icontains=q) | Q(note__icontains=q))
    return qs


def in_period(qs, start: date | None, end: date | None):
    if start is not None:
        qs = qs.filter(date__gte=start)
    if end is not None:
        qs = qs.filter(date__lte=end)
    return qs


def period_for(params, today: date):
    """The dashboard's periods. "All time" has no end, so future-dated entries count too."""
    period = resolve_period(params, today)
    end = None if period.key == "all" else period.end
    return period, end


def _totals(qs) -> dict:
    rows = qs.values("kind").annotate(total=Sum("amount_default"), n=Count("id"))
    by_kind = {r["kind"]: r for r in rows}
    income = by_kind.get(Kind.INCOME, {}).get("total") or Decimal(0)
    expense = by_kind.get(Kind.EXPENSE, {}).get("total") or Decimal(0)
    count = sum(r["n"] for r in rows)
    return {"income": float(income), "expense": float(expense), "balance": float(income - expense), "count": count}


def summary(organization, params, today: date) -> dict:
    base = filter_entries(Entry.objects.filter(organization=organization), params)
    period, end = period_for(params, today)
    current = in_period(base, period.start, end)
    data = {"default_currency": default_currency(organization).pk}

    totals = _totals(current)
    data["totals"] = totals
    data["previous"] = _totals(in_period(base, *period.previous)) if period.previous else None

    on_events = current.filter(kind=Kind.EXPENSE, event__isnull=False)
    agg = on_events.aggregate(total=Sum("amount_default"), events=Count("event", distinct=True))
    data["events"] = {"expense": float(agg["total"] or 0), "count": agg["events"]}

    data["by_category"] = [
        {
            "id": r["category_id"],
            "kind": r["kind"],
            "name_en": r["category__name_en"],
            "name_ar": r["category__name_ar"],
            "color": r["category__color"],
            "total": float(r["total"]),
            "count": r["n"],
        }
        for r in current.values("category_id", "kind", "category__name_en", "category__name_ar", "category__color")
        .annotate(total=Sum("amount_default"), n=Count("id"))
        .order_by("-total")
    ]

    by_currency: dict[int, dict] = {}
    for r in current.values("currency_id", "kind").annotate(native=Sum("amount"), converted=Sum("amount_default")):
        row = by_currency.setdefault(
            r["currency_id"],
            {"currency": r["currency_id"], "income": 0.0, "expense": 0.0, "net": 0.0, "net_default": 0.0},
        )
        sign = 1 if r["kind"] == Kind.INCOME else -1
        row[r["kind"]] = float(r["native"])
        row["net"] += sign * float(r["native"])
        row["net_default"] += sign * float(r["converted"])
    data["by_currency"] = list(by_currency.values())

    if period.start is None:
        period.start = current.aggregate(first=Min("date"))["first"] or today
    data["period"] = period.as_dict()
    return data
