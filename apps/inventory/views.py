from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Count, Prefetch, Q, RestrictedError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.events import services as event_services
from apps.events.models import Event
from apps.members.models import Member, MemberStatus
from apps.permissions.drf import IsOrgMember, get_permission_service

from . import services
from .models import (
    GONE,
    NEEDS_CARE,
    BorrowerType,
    Category,
    ExternalBorrower,
    Item,
    ItemKind,
    ItemLog,
    Loan,
    LoanLine,
    Location,
    Piece,
    Reservation,
    StockTake,
)
from .serializers import (
    AddPiecesSerializer,
    CategorySerializer,
    CheckOutSerializer,
    ConditionSerializer,
    ExternalBorrowerSerializer,
    ItemSerializer,
    LoanSerializer,
    LocationSerializer,
    LogSerializer,
    PieceSerializer,
    ReserveSerializer,
    ReturnSerializer,
    StockActionSerializer,
    StockTakeLineSerializer,
    borrower_payload,
    user_name,
)

VIEW, CHECK_OUT, MANAGE = "inventory.view", "inventory.check_out", "inventory.manage"


def require(request, *codenames):
    service = get_permission_service(request)
    if not any(service.has_perm(c) for c in codenames):
        raise PermissionDenied()


class InventoryMixin(TenantScopedMixin):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]
    # action -> permissions (any of them is enough)
    rights = {"list": (VIEW,), "retrieve": (VIEW,), "*": (MANAGE,)}

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require(request, *self.rights.get(self.action, self.rights["*"]))


# ---------------------------------------------------------------- settings lists


class CategoryViewSet(InventoryMixin, viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    pagination_class = None

    def get_queryset(self):
        return Category.objects.filter(organization=self.organization).annotate(item_count=Count("items"))

    def perform_create(self, serializer):
        last = Category.objects.filter(organization=self.organization).order_by("-order").first()
        obj = serializer.save(organization=self.organization, order=(last.order + 1) if last else 0)
        log_action(self.request, "inventory.category_created", organization=self.organization, target=obj)


class LocationViewSet(InventoryMixin, viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    pagination_class = None

    def get_queryset(self):
        return Location.objects.filter(organization=self.organization).annotate(item_count=Count("items"))

    def perform_create(self, serializer):
        obj = serializer.save(organization=self.organization)
        log_action(self.request, "inventory.location_created", organization=self.organization, target=obj)


class ExternalBorrowerViewSet(InventoryMixin, viewsets.ModelViewSet):
    serializer_class = ExternalBorrowerSerializer
    pagination_class = None
    rights = {"list": (VIEW,), "retrieve": (VIEW,), "create": (CHECK_OUT, MANAGE), "*": (MANAGE,)}

    def get_queryset(self):
        qs = ExternalBorrower.objects.filter(organization=self.organization)
        q = self.request.query_params.get("search", "").strip()
        return qs.filter(Q(name__icontains=q) | Q(contact_person__icontains=q)) if q else qs

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except RestrictedError:
            raise ValidationError(_("They have borrowed equipment before, so they're kept for the history.")) from None


# ---------------------------------------------------------------- items


def item_statuses(items, organization) -> dict[int, dict]:
    """Status of many items with a fixed number of queries."""
    out_pieces: dict[int, set] = defaultdict(set)
    out_qty: dict[int, int] = defaultdict(int)
    for line in services.open_lines(organization).values(
        "item_id", "piece_id", "quantity", "returned_quantity", "lost_quantity"
    ):
        if line["piece_id"]:
            out_pieces[line["item_id"]].add(line["piece_id"])
        else:
            out_qty[line["item_id"]] += line["quantity"] - line["returned_quantity"] - line["lost_quantity"]
    result = {}
    for item in items:
        if item.kind == ItemKind.TRACKED:
            pieces = [p for p in item.pieces.all() if p.condition not in GONE]
            n_out = sum(1 for p in pieces if p.pk in out_pieces[item.pk])
            result[item.pk] = {
                "total": len(pieces),
                "out": n_out,
                "available": len(pieces) - n_out,
                "needs_care": sum(1 for p in pieces if p.condition in NEEDS_CARE),
                "low": False,
            }
        elif item.kind == ItemKind.COUNTED:
            out = out_qty[item.pk]
            result[item.pk] = {
                "total": item.quantity,
                "out": out,
                "available": max(item.quantity - out, 0),
                "needs_care": item.damaged_quantity,
                "low": False,
            }
        else:
            low = item.min_stock is not None and item.quantity < item.min_stock
            result[item.pk] = {
                "total": item.quantity,
                "out": 0,
                "available": item.quantity,
                "needs_care": 0,
                "low": low,
            }
    return result


def loan_queryset():
    return Loan.objects.select_related("event", "unit", "member", "external", "checked_out_by").prefetch_related(
        Prefetch("lines", queryset=LoanLine.objects.select_related("item", "piece", "checked_in_by"))
    )


class ItemViewSet(InventoryMixin, viewsets.ModelViewSet):
    serializer_class = ItemSerializer
    rights = {
        "list": (VIEW, CHECK_OUT),
        "retrieve": (VIEW, CHECK_OUT),
        "history": (VIEW,),
        "availability": (VIEW, CHECK_OUT),
        "*": (MANAGE,),
    }

    def get_queryset(self):
        qs = (
            Item.objects.filter(organization=self.organization)
            .select_related("category", "location")
            .prefetch_related("pieces")
        )
        p = self.request.query_params
        if self.action == "list":
            if p.get("archived") not in ("1", "true"):
                qs = qs.filter(is_archived=False)
            if p.get("search"):
                q = p["search"].strip()
                qs = qs.filter(
                    Q(name_en__icontains=q)
                    | Q(name_ar__icontains=q)
                    | Q(brand__icontains=q)
                    | Q(pieces__code__iexact=q)
                ).distinct()
            for key in ("category", "location", "kind"):
                if p.get(key):
                    qs = qs.filter(**{key: p[key]})
        return qs.order_by("name_en")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "statuses": getattr(self, "_statuses", {})}

    def list(self, request, *args, **kwargs):
        items = list(self.get_queryset())
        self._statuses = item_statuses(items, self.organization)
        p = request.query_params
        if p.get("low_stock") in ("1", "true"):
            items = [i for i in items if self._statuses[i.pk]["low"]]
        if p.get("needs_care") in ("1", "true"):
            items = [i for i in items if self._statuses[i.pk]["needs_care"]]
        return Response(ItemSerializer(items, many=True, context=self.get_serializer_context()).data)

    def detail_payload(self, item):
        item = self.get_queryset().get(pk=item.pk)
        self._statuses = item_statuses([item], self.organization)
        data = ItemSerializer(item, context=self.get_serializer_context()).data
        out = services.pieces_out(item)
        today = timezone.localdate()
        upcoming = list(
            Reservation.objects.filter(item=item, loan_line__isnull=True, event__end_date__gte=today)
            .exclude(event__status="cancelled")
            .select_related("event")
            .order_by("event__start_date")
        )
        pieces = []
        for piece in item.pieces.all():
            line = out.get(piece.pk)
            reserved = next((r for r in upcoming if r.piece_id == piece.pk), None)
            pieces.append(
                {
                    **PieceSerializer(piece).data,
                    "loan": None
                    if line is None
                    else {
                        "id": line.loan_id,
                        "borrower_type": line.loan.borrower_type,
                        "borrower": borrower_payload(line.loan),
                        "due_date": line.loan.due_date,
                        "overdue": bool(line.loan.due_date and line.loan.due_date < today),
                    },
                    "reserved_for": None
                    if reserved is None
                    else {
                        "event": reserved.event_id,
                        "title": reserved.event.title,
                        "start_date": reserved.event.start_date,
                        "end_date": reserved.event.end_date,
                    },
                }
            )
        bookings = defaultdict(lambda: {"quantity": 0})
        for r in upcoming:
            b = bookings[r.event_id]
            b.update(event=r.event_id, title=r.event.title, start_date=r.event.start_date, end_date=r.event.end_date)
            b["quantity"] += 1 if r.piece_id else r.quantity
        open_loans = loan_queryset().filter(closed_at__isnull=True, lines__item=item).distinct()
        data.update(
            pieces=pieces,
            reservations=sorted(bookings.values(), key=lambda b: b["start_date"]),
            open_loans=LoanSerializer(open_loans, many=True).data,
        )
        return data

    def retrieve(self, request, *args, **kwargs):
        return Response(self.detail_payload(self.get_object()))

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count = serializer.validated_data.pop("pieces_count", 0)
        with transaction.atomic():
            item = serializer.save(organization=self.organization)
            if item.kind == ItemKind.TRACKED:
                item.quantity = 0
                item.save(update_fields=["quantity"])
                if count:
                    services.add_pieces(item, count, request.user)
            else:
                services.log(item, "added", request.user, quantity=item.quantity)
        log_action(request, "inventory.item_created", organization=self.organization, target=item)
        return Response(self.detail_payload(item), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        item = self.get_object()
        serializer = self.get_serializer(item, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.validated_data.pop("pieces_count", None)
        serializer.save()
        log_action(request, "inventory.item_updated", organization=self.organization, target=item)
        return Response(self.detail_payload(item))

    def destroy(self, request, *args, **kwargs):
        """Items with history are archived; brand-new mistakes are deleted."""
        item = self.get_object()
        if item.loan_lines.exists() or item.reservations.exists():
            item.is_archived = True
            item.save(update_fields=["is_archived", "updated_at"])
            log_action(request, "inventory.item_archived", organization=self.organization, target=item)
            return Response(self.detail_payload(item))
        log_action(request, "inventory.item_deleted", organization=self.organization, target=item)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        item = self.get_object()
        logs = item.logs.select_related(
            "actor", "piece", "loan", "loan__event", "loan__unit", "loan__member", "loan__external"
        )
        piece = request.query_params.get("piece")
        if piece:
            logs = logs.filter(piece_id=piece)
        return Response(LogSerializer(logs[:200], many=True).data)

    @action(detail=True, methods=["post"])
    def pieces(self, request, pk=None):
        item = self.get_object()
        serializer = AddPiecesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_pieces(
            item, serializer.validated_data["count"], request.user, serializer.validated_data["condition"]
        )
        log_action(request, "inventory.item_updated", organization=self.organization, target=item)
        return Response(self.detail_payload(item))

    @action(detail=True, methods=["post"], url_path=r"pieces/(?P<piece_id>\d+)/condition")
    def piece_condition(self, request, pk=None, piece_id=None):
        item = self.get_object()
        piece = get_object_or_404(Piece, item=item, pk=piece_id)
        serializer = ConditionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_piece_condition(piece, **serializer.validated_data, actor=request.user)
        log_action(
            request,
            "inventory.condition_changed",
            organization=self.organization,
            target=item,
            changes={"piece": piece.code, "condition": piece.condition},
        )
        return Response(self.detail_payload(item))

    @action(detail=True, methods=["post"])
    def stock(self, request, pk=None):
        item = self.get_object()
        serializer = StockActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.adjust_stock(
            item, data["action"], data["quantity"], actor=request.user, note=data["note"], cost=data.get("cost")
        )
        log_action(
            request,
            "inventory.stock_changed",
            organization=self.organization,
            target=item,
            changes={"action": data["action"], "quantity": data["quantity"]},
        )
        return Response(self.detail_payload(item))

    @action(detail=True, methods=["get"])
    def availability(self, request, pk=None):
        item = self.get_object()
        try:
            start = date.fromisoformat(request.query_params.get("start", ""))
            end = date.fromisoformat(request.query_params.get("end", "")) if request.query_params.get("end") else start
        except ValueError:
            raise ValidationError({"start": [_("Invalid date.")]}) from None
        event = request.query_params.get("exclude_event")
        info = services.availability(item, start, end, exclude_event=event or None)
        return Response(info.__dict__)


# ---------------------------------------------------------------- loans


class LoanViewSet(InventoryMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = LoanSerializer
    rights = {"list": (VIEW, CHECK_OUT), "retrieve": (VIEW, CHECK_OUT), "*": (CHECK_OUT,)}

    def get_queryset(self):
        qs = loan_queryset().filter(organization=self.organization)
        p = self.request.query_params
        state = p.get("status", "open")
        if state == "open":
            qs = qs.filter(closed_at__isnull=True)
        elif state == "closed":
            qs = qs.filter(closed_at__isnull=False)
        elif state == "overdue":
            qs = qs.filter(pk__in=services.overdue_loans(self.organization).values("pk"))
        if p.get("borrower_type"):
            qs = qs.filter(borrower_type=p["borrower_type"])
        for key in ("event", "unit", "member", "external"):
            if p.get(key):
                qs = qs.filter(**{key: p[key]})
        if p.get("item"):
            qs = qs.filter(lines__item=p["item"]).distinct()
        if p.get("search"):
            q = p["search"].strip()
            qs = qs.filter(
                Q(event__title__icontains=q)
                | Q(unit__name_en__icontains=q)
                | Q(member__first_name__icontains=q)
                | Q(member__last_name__icontains=q)
                | Q(external__name__icontains=q)
                | Q(lines__item__name_en__icontains=q)
                | Q(lines__piece__code__iexact=q)
            ).distinct()
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CheckOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        org = self.organization
        with transaction.atomic():
            if data["borrower_type"] == BorrowerType.EXTERNAL and not data.get("external") and data.get("new_external"):
                data["external"] = ExternalBorrower.objects.create(organization=org, **data["new_external"])
            borrower = services.validate_borrower(org, data)
            lines = [
                {"item": line["item"], "pieces": line["pieces"], "quantity": line["quantity"]} for line in data["lines"]
            ]
            loan, warnings = services.check_out(
                org, borrower=borrower, lines=lines, note=data["note"], actor=request.user
            )
        log_action(
            request,
            "inventory.checked_out",
            organization=org,
            target=loan,
            changes={"lines": loan.lines.count(), "borrower_type": loan.borrower_type},
        )
        body = LoanSerializer(loan_queryset().get(pk=loan.pk)).data
        body["warnings"] = warnings
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return")
    def return_items(self, request, pk=None):
        loan = self.get_object()
        serializer = ReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.return_lines(
            loan,
            data["returns"],
            returned_by_name=data["returned_by_name"],
            returned_at=data.get("returned_at"),
            actor=request.user,
        )
        log_action(request, "inventory.returned", organization=self.organization, target=loan)
        return Response(LoanSerializer(loan_queryset().get(pk=loan.pk)).data)


# ---------------------------------------------------------------- overview, lookup, event gear


class SummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        require(request, VIEW, CHECK_OUT)
        org = get_active_membership(request).organization
        items = list(Item.objects.filter(organization=org, is_archived=False).prefetch_related("pieces"))
        statuses = item_statuses(items, org)
        overdue = loan_queryset().filter(pk__in=services.overdue_loans(org).values("pk"))
        today = timezone.localdate()
        care = []
        for item in items:
            if item.kind == ItemKind.TRACKED:
                care += [
                    {"item": item.pk, "name_en": item.name_en, "name_ar": item.name_ar, "code": p.code,
                     "condition": p.condition, "note": p.condition_note}
                    for p in item.pieces.all()
                    if p.condition in NEEDS_CARE
                ]  # fmt: skip
            elif item.kind == ItemKind.COUNTED and item.damaged_quantity:
                care.append(
                    {"item": item.pk, "name_en": item.name_en, "name_ar": item.name_ar, "code": "",
                     "condition": "damaged", "note": "", "quantity": item.damaged_quantity}
                )  # fmt: skip
        low = [
            {"item": i.pk, "name_en": i.name_en, "name_ar": i.name_ar, "quantity": i.quantity, "min_stock": i.min_stock}
            for i in items
            if statuses[i.pk]["low"]
        ]
        reserved_week = Reservation.objects.filter(
            organization=org,
            loan_line__isnull=True,
            event__start_date__lte=today + timedelta(days=7),
            event__end_date__gte=today,
        ).count()
        recent = ItemLog.objects.filter(item__organization=org).select_related(
            "item", "piece", "actor", "loan", "loan__event", "loan__unit", "loan__member", "loan__external"
        )[:8]
        return Response(
            {
                "items": len(items),
                "out_now": sum(s["out"] for s in statuses.values()),
                "overdue": overdue.count(),
                "low_stock": len(low),
                "needs_care": len(care),
                "reserved_this_week": reserved_week,
                "overdue_loans": LoanSerializer(overdue[:10], many=True).data,
                "low_items": low,
                "care_items": care[:10],
                "recent": [
                    {**LogSerializer(log).data, "item_name_en": log.item.name_en, "item_name_ar": log.item.name_ar}
                    for log in recent
                ],
            }
        )


class LookupView(APIView):
    """Resolve a scanned QR code (a piece code, or item-<id>) to an item and piece."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        require(request, VIEW, CHECK_OUT)
        org = get_active_membership(request).organization
        code = request.query_params.get("code", "").strip()
        if code.lower().startswith("item-") and code[5:].isdigit():
            item = Item.objects.filter(organization=org, pk=int(code[5:])).first()
            if item:
                return Response({"item": item.pk, "piece": None})
        piece = Piece.objects.filter(item__organization=org, code__iexact=code).first()
        if piece is None:
            return Response({"detail": _("No item with this code.")}, status=status.HTTP_404_NOT_FOUND)
        return Response({"item": piece.item_id, "piece": piece.pk})


def event_rights(request, event):
    service = get_permission_service(request)
    can_edit = event_services.can_manage(service, event) or service.has_perm(MANAGE)
    can_view = can_edit or service.has_perm(VIEW) or event in event_services.visible_events(service, event.organization)
    return can_view, can_edit, service.has_perm(CHECK_OUT)


class EventGearView(APIView):
    """The gear list of an event: GET it, POST to set an item's reservation (quantity 0 removes it)."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get_event(self, request, event_id):
        org = get_active_membership(request).organization
        return get_object_or_404(Event, organization=org, pk=event_id)

    def payload(self, request, event):
        _view, can_edit, can_check_out = event_rights(request, event)
        return {"rows": services.gear_list(event), "can_edit": can_edit, "can_check_out": can_check_out}

    def get(self, request, event_id):
        event = self.get_event(request, event_id)
        can_view, _e, _c = event_rights(request, event)
        if not can_view:
            raise PermissionDenied()
        return Response(self.payload(request, event))

    def post(self, request, event_id):
        event = self.get_event(request, event_id)
        _v, can_edit, _c = event_rights(request, event)
        if not can_edit:
            raise PermissionDenied()
        serializer = ReserveSerializer(data={**request.data, "event": event.pk})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.reserve(event, data["item"], quantity=data["quantity"], pieces=data["pieces"], actor=request.user)
        log_action(request, "inventory.reserved", organization=event.organization, target=event)
        return Response(self.payload(request, event))


class EventGearCheckOutView(EventGearView):
    def post(self, request, event_id):
        event = self.get_event(request, event_id)
        require(request, CHECK_OUT)
        loan, warnings = services.check_out_event_gear(event, actor=request.user)
        log_action(request, "inventory.checked_out", organization=event.organization, target=loan)
        return Response({**self.payload(request, event), "loan": loan.pk, "warnings": warnings})


# ---------------------------------------------------------------- stock-take


class StockTakeViewSet(InventoryMixin, viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch"]
    rights = {"list": (VIEW,), "retrieve": (VIEW,), "*": (MANAGE,)}

    def get_queryset(self):
        return StockTake.objects.filter(organization=self.organization).select_related(
            "location", "started_by", "finished_by"
        )

    def payload(self, st):
        lines = st.lines.select_related("item", "piece")
        counted = sum(1 for line in lines if line.counted is not None)
        return {
            "id": st.pk,
            "location": {"id": st.location_id, "name_en": st.location.name_en, "name_ar": st.location.name_ar}
            if st.location
            else None,
            "started_by": user_name(st.started_by),
            "started_at": st.created_at,
            "finished_by": user_name(st.finished_by),
            "finished_at": st.finished_at,
            "total": len(lines),
            "counted": counted,
            "differences": sum(1 for line in lines if line.counted is not None and line.counted != line.expected),
            "lines": StockTakeLineSerializer(lines, many=True).data,
        }

    def list(self, request, *args, **kwargs):
        return Response(
            [{k: v for k, v in self.payload(st).items() if k != "lines"} for st in self.get_queryset()[:20]]
        )

    def retrieve(self, request, *args, **kwargs):
        return Response(self.payload(self.get_object()))

    def create(self, request, *args, **kwargs):
        location = request.data.get("location")
        location = get_object_or_404(Location, organization=self.organization, pk=location) if location else None
        st = services.start_stocktake(self.organization, location, request.user)
        return Response(self.payload(st), status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        """Body: {"counts": {line_id: counted | null}}"""
        st = self.get_object()
        if st.finished_at:
            raise ValidationError(_("This stock-take is already finished."))
        counts = request.data.get("counts", {})
        lines = {line.pk: line for line in st.lines.all()}
        for key, value in counts.items():
            line = lines.get(int(key))
            if line is None:
                continue
            if value is not None and (not isinstance(value, int) or value < 0 or (line.piece_id and value > 1)):
                raise ValidationError({"counts": [_("Invalid count.")]})
            line.counted = value
            line.save(update_fields=["counted"])
        return Response(self.payload(st))

    @action(detail=True, methods=["post"])
    def finish(self, request, pk=None):
        st = self.get_object()
        result = services.finish_stocktake(st, request.user)
        log_action(request, "inventory.stocktake_finished", organization=self.organization, target=st, changes=result)
        return Response(self.payload(st))


# ---------------------------------------------------------------- members & parents


class MyGearView(APIView):
    """Equipment checked out to the current member or their children."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        membership = get_active_membership(request)
        me, _guardian, children = event_services.my_people(membership)
        ids = list(children) + ([me.pk] if me else [])
        loans = loan_queryset().filter(
            organization=membership.organization, borrower_type=BorrowerType.MEMBER, member_id__in=ids
        )
        open_loans = [loan for loan in loans.filter(closed_at__isnull=True)]
        past = list(loans.filter(closed_at__isnull=False)[:20])
        return Response(
            {
                "open": LoanSerializer(open_loans, many=True).data,
                "past": LoanSerializer(past, many=True).data,
            }
        )


class BorrowerOptionsView(APIView):
    """Members and events to lend to, for people who can check out but can't see the member list."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        require(request, CHECK_OUT)
        org = get_active_membership(request).organization
        q = request.query_params.get("search", "").strip()
        members = Member.objects.filter(organization=org).exclude(status=MemberStatus.ALUMNI)
        if q:
            members = members.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(first_name_ar__icontains=q)
                | Q(last_name_ar__icontains=q)
            )
        today = timezone.localdate()
        events = (
            Event.objects.filter(organization=org, end_date__gte=today - timedelta(days=7))
            .exclude(status="cancelled")
            .order_by("start_date")[:60]
        )
        return Response(
            {
                "members": [
                    {"id": m.pk, "full_name": m.full_name, "name_ar": m.name_ar, "phone": m.phone}
                    for m in members.order_by("first_name", "last_name")[:50]
                ],
                "events": [
                    {"id": e.pk, "title": e.title, "start_date": e.start_date, "end_date": e.end_date} for e in events
                ],
            }
        )
