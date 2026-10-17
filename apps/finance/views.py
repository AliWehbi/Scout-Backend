from django.db.models import Count, RestrictedError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.events import services as event_services
from apps.events.models import Event
from apps.permissions.drf import IsOrgMember, get_permission_service

from . import services
from .models import Category, Currency, Entry
from .presets import PRESETS, preset
from .serializers import CategorySerializer, CurrencySerializer, EntrySerializer

VIEW, MANAGE = "finance.view", "finance.manage"


def require(request, *codenames):
    service = get_permission_service(request)
    if not any(service.has_perm(c) for c in codenames):
        raise PermissionDenied()


class FinanceMixin(TenantScopedMixin):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]
    # action -> permissions (any of them is enough)
    rights = {"list": (VIEW, MANAGE), "retrieve": (VIEW, MANAGE), "presets": (VIEW, MANAGE), "*": (MANAGE,)}

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require(request, *self.rights.get(self.action, self.rights["*"]))

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "organization": self.organization}


def _changes(instance, fields) -> dict:
    return {f: str(getattr(instance, f)) for f in fields}


class CurrencyViewSet(FinanceMixin, viewsets.ModelViewSet):
    serializer_class = CurrencySerializer
    pagination_class = None

    def get_queryset(self):
        services.default_currency(self.organization)  # every organization has one
        return (
            Currency.objects.filter(organization=self.organization)
            .select_related("rate_updated_by")
            .annotate(entry_count=Count("entries"))
            .order_by("-is_default", "order", "code")
        )

    def perform_create(self, serializer):
        last = Currency.objects.filter(organization=self.organization).order_by("-order").first()
        obj = serializer.save(
            organization=self.organization,
            order=(last.order + 1) if last else 0,
            rate_updated_at=timezone.now(),
            rate_updated_by=self.request.user,
        )
        log_action(
            self.request,
            "finance.currency_created",
            organization=self.organization,
            target=obj,
            changes=_changes(obj, ["rate"]),
        )

    def perform_update(self, serializer):
        old_rate = serializer.instance.rate
        extra = {}
        new_rate = serializer.validated_data.get("rate")
        if new_rate is not None and new_rate != old_rate:
            extra = {"rate_updated_at": timezone.now(), "rate_updated_by": self.request.user}
        obj = serializer.save(**extra)
        changes = {"rate": [str(old_rate), str(obj.rate)]} if extra else {}
        log_action(
            self.request, "finance.currency_updated", organization=self.organization, target=obj, changes=changes
        )

    def perform_destroy(self, instance):
        if instance.is_default:
            raise ValidationError({"detail": _("The default currency can't be deleted.")})
        try:
            instance.delete()
        except RestrictedError:
            raise ValidationError({"detail": _("Entries use this currency, so it can only be hidden.")}) from None
        log_action(self.request, "finance.currency_deleted", organization=self.organization, target=instance)

    @action(detail=False, methods=["get"])
    def presets(self, request):
        """Known currencies with their English and Arabic names and symbols, for the add form."""
        return Response([preset(code) for code in PRESETS])

    @action(detail=True, methods=["post"], url_path="make-default")
    def make_default(self, request, pk=None):
        currency = self.get_object()
        services.make_default(currency, request.user)
        log_action(request, "finance.currency_default", organization=self.organization, target=currency)
        return Response(CurrencySerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data)


class CategoryViewSet(FinanceMixin, viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    pagination_class = None
    rights = {"list": (VIEW, MANAGE), "retrieve": (VIEW, MANAGE), "*": (MANAGE,)}

    def get_queryset(self):
        services.default_currency(self.organization)
        qs = (
            Category.objects.filter(organization=self.organization)
            .annotate(entry_count=Count("entries"))
            .order_by("kind", "order", "name_en")
        )
        kind = self.request.query_params.get("kind")
        return qs.filter(kind=kind) if kind else qs

    def perform_create(self, serializer):
        kind = serializer.validated_data["kind"]
        last = Category.objects.filter(organization=self.organization, kind=kind).order_by("-order").first()
        obj = serializer.save(organization=self.organization, order=(last.order + 1) if last else 0)
        log_action(self.request, "finance.category_created", organization=self.organization, target=obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        log_action(self.request, "finance.category_updated", organization=self.organization, target=obj)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except RestrictedError:
            raise ValidationError({"detail": _("Entries use this category, so it can only be hidden.")}) from None
        log_action(self.request, "finance.category_deleted", organization=self.organization, target=instance)

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValidationError({"ids": [_("Send the category ids in their new order.")]})
        cats = {c.pk: c for c in Category.objects.filter(organization=self.organization, pk__in=ids)}
        for order, pk in enumerate(ids):
            if pk in cats:
                cats[pk].order = order
        Category.objects.bulk_update(cats.values(), ["order"])
        return Response(CategorySerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data)


class EntryViewSet(FinanceMixin, viewsets.ModelViewSet):
    serializer_class = EntrySerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        qs = Entry.objects.filter(organization=self.organization).select_related(
            "currency", "category", "event", "created_by", "updated_by"
        )
        if self.action == "list":
            p = self.request.query_params
            qs = services.filter_entries(qs, p)
            period, end = services.period_for(p, timezone.localdate())
            qs = services.in_period(qs, period.start, end)
        return qs

    def perform_create(self, serializer):
        obj = serializer.save(
            organization=self.organization, created_by=self.request.user, updated_by=self.request.user
        )
        log_action(
            self.request,
            "finance.entry_created",
            organization=self.organization,
            target=obj,
            changes=_changes(obj, ["kind", "amount", "currency", "date"]),
        )

    def perform_update(self, serializer):
        obj = serializer.save(updated_by=self.request.user)
        log_action(
            self.request,
            "finance.entry_updated",
            organization=self.organization,
            target=obj,
            changes=_changes(obj, ["kind", "amount", "currency", "date"]),
        )

    def perform_destroy(self, instance):
        if instance.event_id:
            event_services.require_open(instance.event)
        log_action(
            self.request,
            "finance.entry_deleted",
            organization=self.organization,
            target=instance,
            changes=_changes(instance, ["kind", "amount", "currency", "date"]),
        )
        if instance.receipt:
            instance.receipt.delete(save=False)
        instance.delete()


class SummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        require(request, VIEW, MANAGE)
        org = get_active_membership(request).organization
        return Response(services.summary(org, request.query_params, timezone.localdate()))


class EventOptionsView(APIView):
    """Events to link entries to. Finance users may not have access to the events pages."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        require(request, VIEW, MANAGE)
        org = get_active_membership(request).organization
        qs = Event.objects.filter(organization=org).order_by("-start_date", "-id")
        if q := request.query_params.get("search", "").strip():
            qs = qs.filter(title__icontains=q)
        return Response(
            [
                {
                    "id": e.pk,
                    "title": e.title,
                    "start_date": e.start_date,
                    "end_date": e.end_date,
                    "closed": e.is_closed,
                    "cancelled": e.status == "cancelled",
                }
                for e in qs[:300]
            ]
        )
