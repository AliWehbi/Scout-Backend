"""
Inventory rules: availability, check-out and return, reservations for events, condition and
stock changes, stock-takes and alerts.

Warnings are soft: lending or booking a damaged piece, or booking more than is free, is allowed
and returns warnings. Only things that are physically impossible are refused (lending a piece
that is already out, more than is in the store, or a lost or retired piece).
"""

from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from apps.events.models import Event, EventStatus

from .models import (
    GONE,
    NEEDS_CARE,
    BorrowerType,
    Condition,
    Item,
    ItemKind,
    ItemLog,
    Loan,
    LoanLine,
    LogKind,
    Piece,
    Reservation,
    StockTake,
    StockTakeLine,
)


def log(item, kind, actor, *, piece=None, loan=None, quantity=None, cost=None, note="", at=None):
    return ItemLog.objects.create(
        item=item,
        piece=piece,
        loan=loan,
        kind=kind,
        quantity=quantity,
        cost=cost,
        note=note[:255],
        actor=actor,
        at=at or timezone.now(),
    )


def piece_code(item: Item, number: int) -> str:
    return f"{item.code_prefix}-{number:02d}" if item.code_prefix else f"#{number}"


@transaction.atomic
def add_pieces(item: Item, count: int, actor, condition=Condition.NEW) -> list[Piece]:
    if item.kind != ItemKind.TRACKED:
        raise ValidationError(_("Only one-by-one items have pieces."))
    last = item.pieces.order_by("-number").first()
    start = (last.number if last else 0) + 1
    pieces = [
        Piece(item=item, number=n, code=piece_code(item, n), condition=condition) for n in range(start, start + count)
    ]
    Piece.objects.bulk_create(pieces)
    created = list(item.pieces.filter(number__gte=start))
    for piece in created:
        log(item, LogKind.ADDED, actor, piece=piece)
    return created


# ---------------------------------------------------------------- what's where


def open_lines(organization=None):
    qs = LoanLine.objects.filter(loan__closed_at__isnull=True).exclude(item__kind=ItemKind.CONSUMABLE)
    if organization is not None:
        qs = qs.filter(loan__organization=organization)
    return qs.filter(quantity__gt=F("returned_quantity") + F("lost_quantity"))


def pieces_out(item: Item) -> dict[int, LoanLine]:
    lines = (
        open_lines()
        .filter(item=item, piece__isnull=False)
        .select_related("loan", "loan__event", "loan__unit", "loan__member", "loan__external")
    )
    return {line.piece_id: line for line in lines}


def quantity_out(item: Item) -> int:
    agg = (
        open_lines()
        .filter(item=item)
        .aggregate(q=Sum("quantity"), r=Sum("returned_quantity"), lost=Sum("lost_quantity"))
    )
    return (agg["q"] or 0) - (agg["r"] or 0) - (agg["lost"] or 0)


def active_pieces(item: Item):
    return item.pieces.exclude(condition__in=GONE)


def status(item: Item) -> dict:
    """Counts for lists: total, out, available now, needing care, low stock."""
    if item.kind == ItemKind.TRACKED:
        pieces = list(active_pieces(item))
        out = pieces_out(item)
        total = len(pieces)
        n_out = sum(1 for p in pieces if p.pk in out)
        care = sum(1 for p in pieces if p.condition in NEEDS_CARE)
        return {"total": total, "out": n_out, "available": total - n_out, "needs_care": care, "low": False}
    if item.kind == ItemKind.COUNTED:
        out = quantity_out(item)
        return {
            "total": item.quantity,
            "out": out,
            "available": max(item.quantity - out, 0),
            "needs_care": item.damaged_quantity,
            "low": False,
        }
    low = item.min_stock is not None and item.quantity < item.min_stock
    return {"total": item.quantity, "out": 0, "available": item.quantity, "needs_care": 0, "low": low}


def overlapping_reservations(item: Item, start: date, end: date, exclude_event=None):
    qs = Reservation.objects.filter(
        item=item,
        loan_line__isnull=True,
        event__start_date__lte=end,
        event__end_date__gte=start,
    ).exclude(event__status=EventStatus.CANCELLED)
    if exclude_event is not None:
        qs = qs.exclude(event=exclude_event)
    return qs.select_related("event", "piece")


@dataclass
class Availability:
    total: int
    free: int
    free_pieces: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def availability(item: Item, start: date, end: date, exclude_event=None) -> Availability:
    """What is free for these dates, after loans still out and other events' bookings."""
    bookings = list(overlapping_reservations(item, start, end, exclude_event))
    booked_pieces = {r.piece_id for r in bookings if r.piece_id}
    booked_qty = sum(r.quantity for r in bookings if not r.piece_id)
    if item.kind == ItemKind.TRACKED:
        pieces = list(active_pieces(item))
        out = {
            pid for pid, line in pieces_out(item).items() if line.loan.due_date is None or line.loan.due_date >= start
        }
        free = [p for p in pieces if p.pk not in out and p.pk not in booked_pieces]
        warnings = [
            _("%(code)s is %(condition)s") % {"code": p.code, "condition": p.get_condition_display().lower()}
            for p in free
            if p.condition in NEEDS_CARE
        ]
        return Availability(len(pieces), max(len(free) - booked_qty, 0), [p.pk for p in free], warnings)
    if item.kind == ItemKind.COUNTED:
        lines = open_lines().filter(item=item).filter(Q(loan__due_date__isnull=True) | Q(loan__due_date__gte=start))
        out = sum(line.outstanding for line in lines)
        free = max(item.quantity - out - booked_qty, 0)
        warnings = [_("%(n)s of them are damaged") % {"n": item.damaged_quantity}] if item.damaged_quantity else []
        return Availability(item.quantity, free, [], warnings)
    return Availability(item.quantity, max(item.quantity - booked_qty, 0), [], [])


# ---------------------------------------------------------------- check-out & return


def validate_borrower(organization, data: dict) -> dict:
    kind = data["borrower_type"]
    target = data.get(kind)
    if kind == BorrowerType.EXTERNAL:
        if target is None:
            raise ValidationError({"external": [_("Choose or add who is borrowing.")]})
    elif target is None:
        raise ValidationError({kind: [_("This field is required.")]})
    if getattr(target, "organization_id", None) != organization.pk:
        raise ValidationError({kind: [_("Unknown borrower.")]})
    due = data.get("due_date")
    if due is None and kind == BorrowerType.EVENT:
        due = target.end_date
    if due is None and kind in (BorrowerType.MEMBER, BorrowerType.EXTERNAL):
        raise ValidationError({"due_date": [_("A due date is required for members and external borrowers.")]})
    return {"borrower_type": kind, kind: target, "due_date": due}


@transaction.atomic
def check_out(organization, *, borrower: dict, lines: list[dict], note: str, actor, at=None) -> tuple[Loan, list[str]]:
    """
    lines: [{"item": Item, "pieces": [Piece]}] for one-by-one items, [{"item": Item, "quantity": n}] otherwise.
    Returns the loan and soft warnings.
    """
    if not lines:
        raise ValidationError({"lines": [_("Add at least one item.")]})
    at = at or timezone.now()
    loan = Loan.objects.create(
        organization=organization, note=note, checked_out_by=actor, checked_out_at=at, **borrower
    )
    warnings: list[str] = []
    for spec in lines:
        item = Item.objects.select_for_update().get(pk=spec["item"].pk)
        if item.organization_id != organization.pk:
            raise ValidationError({"lines": [_("Unknown item.")]})
        if item.kind == ItemKind.TRACKED:
            pieces = spec.get("pieces") or []
            if not pieces:
                raise ValidationError({"lines": [_("Choose which pieces of %(item)s.") % {"item": item.name_en}]})
            out = pieces_out(item)
            for piece in pieces:
                if piece.item_id != item.pk:
                    raise ValidationError({"lines": [_("Unknown piece.")]})
                if piece.pk in out:
                    raise ValidationError({"lines": [_("%(code)s is already checked out.") % {"code": piece.code}]})
                if piece.condition in GONE:
                    raise ValidationError(
                        {"lines": [_("%(code)s is %(c)s.") % {"code": piece.code, "c": piece.get_condition_display()}]}
                    )
                if piece.condition in NEEDS_CARE:
                    warnings.append(
                        _("%(code)s is %(c)s.") % {"code": piece.code, "c": piece.get_condition_display().lower()}
                    )
                LoanLine.objects.create(loan=loan, item=item, piece=piece, quantity=1)
                log(item, LogKind.CHECKED_OUT, actor, piece=piece, loan=loan, quantity=1, at=at)
                _warn_bookings(item, loan, warnings, piece=piece)
            continue
        quantity = int(spec.get("quantity") or 0)
        if quantity < 1:
            raise ValidationError({"lines": [_("Enter how many %(item)s.") % {"item": item.name_en}]})
        if item.kind == ItemKind.COUNTED:
            free = item.quantity - quantity_out(item)
            if quantity > free:
                raise ValidationError(
                    {"lines": [_("Only %(n)s %(item)s in the store.") % {"n": free, "item": item.name_en}]}
                )
            if item.damaged_quantity:
                warnings.append(_("Some %(item)s are damaged.") % {"item": item.name_en})
            _warn_bookings(item, loan, warnings, quantity=quantity)
        else:
            if quantity > item.quantity:
                raise ValidationError(
                    {"lines": [_("Only %(n)s %(item)s in stock.") % {"n": item.quantity, "item": item.name_en}]}
                )
            item.quantity -= quantity
            item.save(update_fields=["quantity", "updated_at"])
        LoanLine.objects.create(loan=loan, item=item, quantity=quantity)
        log(item, LogKind.CHECKED_OUT, actor, loan=loan, quantity=quantity, at=at)
    close_if_done(loan)
    return loan, warnings


def _warn_bookings(item, loan, warnings, *, piece=None, quantity=0):
    """Soft warning when what leaves now is booked for an event before it comes back."""
    end = loan.due_date or date.max
    for r in overlapping_reservations(item, timezone.localdate(), end, exclude_event=loan.event):
        if piece is not None and r.piece_id == piece.pk:
            warnings.append(_("%(code)s is booked for %(event)s.") % {"code": piece.code, "event": r.event.title})
        elif piece is None and quantity and r.piece_id is None:
            warnings.append(
                _("%(item)s are also booked for %(event)s.") % {"item": item.name_en, "event": r.event.title}
            )


def close_if_done(loan: Loan) -> None:
    lines = list(loan.lines.select_related("item"))
    if all(line.outstanding == 0 for line in lines) and loan.closed_at is None:
        loan.closed_at = timezone.now()
        loan.save(update_fields=["closed_at", "updated_at"])


@transaction.atomic
def return_lines(loan: Loan, returns: list[dict], *, returned_by_name: str, returned_at, actor) -> Loan:
    """
    returns: [{"line": LoanLine, "returned": n, "lost": n, "damaged": n, "condition": str, "note": str}]
    One-by-one pieces: returned 1 (with a condition) or lost 1. Partial returns are fine.
    """
    now = timezone.now()
    returned_at = returned_at or now
    for spec in returns:
        line = LoanLine.objects.select_for_update().select_related("item", "piece").get(pk=spec["line"].pk)
        if line.loan_id != loan.pk:
            raise ValidationError({"returns": [_("Unknown line.")]})
        item = Item.objects.select_for_update().get(pk=line.item_id)
        returned, lost = int(spec.get("returned") or 0), int(spec.get("lost") or 0)
        note = spec.get("note", "")
        if item.kind == ItemKind.CONSUMABLE:
            unused = returned
            if unused > line.quantity - line.returned_quantity:
                raise ValidationError({"returns": [_("More than was taken.")]})
            if unused:
                line.returned_quantity += unused
                item.quantity += unused
                item.save(update_fields=["quantity", "updated_at"])
                log(item, LogKind.RETURNED, actor, loan=loan, quantity=unused, note=note, at=returned_at)
        else:
            if returned + lost > line.outstanding:
                raise ValidationError({"returns": [_("More than is still out.")]})
            if returned + lost == 0:
                continue
            line.returned_quantity += returned
            line.lost_quantity += lost
            if line.piece_id:
                piece = line.piece
                if lost:
                    piece.condition = Condition.LOST
                    piece.condition_note = note
                    log(item, LogKind.LOST, actor, piece=piece, loan=loan, note=note, at=returned_at)
                else:
                    condition = spec.get("condition") or piece.condition
                    if condition in (Condition.LOST, Condition.RETIRED):
                        condition = piece.condition
                    line.return_condition = condition
                    piece.condition = condition
                    piece.condition_note = note if condition in NEEDS_CARE else piece.condition_note
                    log(item, LogKind.RETURNED, actor, piece=piece, loan=loan, note=note, at=returned_at)
                    if condition in NEEDS_CARE:
                        log(item, LogKind.DAMAGED, actor, piece=piece, loan=loan, note=note, at=returned_at)
                piece.save(update_fields=["condition", "condition_note"])
            else:
                damaged = min(int(spec.get("damaged") or 0), returned)
                if returned:
                    log(item, LogKind.RETURNED, actor, loan=loan, quantity=returned, note=note, at=returned_at)
                if damaged:
                    item.damaged_quantity += damaged
                    log(item, LogKind.DAMAGED, actor, loan=loan, quantity=damaged, note=note, at=returned_at)
                if lost:
                    item.quantity = max(item.quantity - lost, 0)
                    item.damaged_quantity = min(item.damaged_quantity, item.quantity)
                    log(item, LogKind.LOST, actor, loan=loan, quantity=lost, note=note, at=returned_at)
                item.save(update_fields=["quantity", "damaged_quantity", "updated_at"])
        line.return_note = note or line.return_note
        line.returned_by_name = returned_by_name
        line.returned_at = returned_at
        line.checked_in_by = actor
        line.checked_in_at = now
        line.save()
    close_if_done(loan)
    return loan


# ---------------------------------------------------------------- condition & stock


@transaction.atomic
def set_piece_condition(piece: Piece, condition: str, *, actor, note="", cost=None) -> Piece:
    kinds = {
        Condition.DAMAGED: LogKind.DAMAGED,
        Condition.UNDER_REPAIR: LogKind.REPAIR,
        Condition.LOST: LogKind.LOST,
        Condition.RETIRED: LogKind.RETIRED,
    }
    was = piece.condition
    if was == condition and not note:
        return piece
    kind = kinds.get(condition, LogKind.REPAIRED if was in NEEDS_CARE else LogKind.UPDATED)
    piece.condition = condition
    piece.condition_note = note
    piece.save(update_fields=["condition", "condition_note"])
    log(piece.item, kind, actor, piece=piece, note=note, cost=cost)
    return piece


@transaction.atomic
def adjust_stock(item: Item, action: str, quantity: int, *, actor, note="", cost=None) -> Item:
    """restock / use (consumables), damaged / repaired / lost / add (counted)."""
    item = Item.objects.select_for_update().get(pk=item.pk)
    if quantity < 1:
        raise ValidationError({"quantity": [_("Must be at least 1.")]})
    if item.kind == ItemKind.CONSUMABLE and action in ("restock", "use"):
        if action == "use" and quantity > item.quantity:
            raise ValidationError({"quantity": [_("Only %(n)s in stock.") % {"n": item.quantity}]})
        item.quantity += quantity if action == "restock" else -quantity
        kind = LogKind.RESTOCKED if action == "restock" else LogKind.ADJUSTED
        log(item, kind, actor, quantity=quantity if action == "restock" else -quantity, note=note, cost=cost)
    elif item.kind == ItemKind.COUNTED and action in ("add", "damaged", "repaired", "lost"):
        in_store = item.quantity - quantity_out(item)
        if action == "add":
            item.quantity += quantity
            log(item, LogKind.ADDED, actor, quantity=quantity, note=note, cost=cost)
        elif action == "damaged":
            if item.damaged_quantity + quantity > in_store:
                raise ValidationError({"quantity": [_("More than is in the store.")]})
            item.damaged_quantity += quantity
            log(item, LogKind.DAMAGED, actor, quantity=quantity, note=note)
        elif action == "repaired":
            if quantity > item.damaged_quantity:
                raise ValidationError({"quantity": [_("Only %(n)s are damaged.") % {"n": item.damaged_quantity}]})
            item.damaged_quantity -= quantity
            log(item, LogKind.REPAIRED, actor, quantity=quantity, note=note, cost=cost)
        else:
            if quantity > in_store:
                raise ValidationError({"quantity": [_("More than is in the store.")]})
            item.quantity -= quantity
            item.damaged_quantity = min(item.damaged_quantity, item.quantity)
            log(item, LogKind.LOST, actor, quantity=quantity, note=note)
    else:
        raise ValidationError({"action": [_("Not possible for this kind of item.")]})
    item.save(update_fields=["quantity", "damaged_quantity", "updated_at"])
    return item


# ---------------------------------------------------------------- reservations


def gear_list(event: Event) -> list[dict]:
    rows = []
    reservations = list(Reservation.objects.filter(event=event).select_related("item", "piece", "loan_line"))
    by_item: dict[int, list[Reservation]] = {}
    for r in reservations:
        by_item.setdefault(r.item_id, []).append(r)
    for rs in by_item.values():
        item = rs[0].item
        info = availability(item, event.start_date, event.end_date, exclude_event=event)
        pieces = [r.piece for r in rs if r.piece_id]
        needed = sum(1 if r.piece_id else r.quantity for r in rs)
        if item.kind == ItemKind.TRACKED:
            free_ids = set(info.free_pieces)
            unavailable = [p for p in pieces if p.pk not in free_ids]
            unassigned = needed - len(pieces)
            ok = not unavailable and unassigned <= info.free - (len(pieces) - len(unavailable))
            warnings = [
                _("%(code)s is %(c)s.") % {"code": p.code, "c": p.get_condition_display().lower()}
                for p in pieces
                if p.condition in NEEDS_CARE
            ]
            warnings += [_("%(code)s is booked or out on these dates.") % {"code": p.code} for p in unavailable]
        else:
            ok = needed <= info.free
            warnings = list(info.warnings)
        rows.append(
            {
                "item": item.pk,
                "name_en": item.name_en,
                "name_ar": item.name_ar,
                "kind": item.kind,
                "needed": needed,
                "free": info.free,
                "total": info.total,
                "ok": ok,
                "warnings": sorted(set(warnings)),
                "checked_out": all(r.loan_line_id for r in rs),
                "reservations": [
                    {"id": r.pk, "piece": r.piece_id, "code": r.piece.code if r.piece else "", "quantity": r.quantity}
                    for r in rs
                ],
            }
        )
    return sorted(rows, key=lambda r: r["name_en"])


@transaction.atomic
def reserve(event: Event, item: Item, *, quantity: int = 0, pieces=(), actor) -> None:
    if item.organization_id != event.organization_id:
        raise ValidationError({"item": [_("Unknown item.")]})
    Reservation.objects.filter(event=event, item=item, loan_line__isnull=True).delete()
    if item.kind == ItemKind.TRACKED and pieces:
        for piece in pieces:
            if piece.item_id != item.pk:
                raise ValidationError({"pieces": [_("Unknown piece.")]})
            Reservation.objects.create(
                organization=event.organization, event=event, item=item, piece=piece, quantity=1, created_by=actor
            )
    elif quantity > 0:
        Reservation.objects.create(
            organization=event.organization, event=event, item=item, quantity=quantity, created_by=actor
        )


@transaction.atomic
def check_out_event_gear(event: Event, *, actor) -> tuple[Loan | None, list[str]]:
    """Turn the event's open reservations into one loan to the event, picking free pieces when needed."""
    pending = list(Reservation.objects.filter(event=event, loan_line__isnull=True).select_related("item", "piece"))
    if not pending:
        raise ValidationError(_("Nothing left to check out for this event."))
    specs: dict[int, dict] = {}
    for r in pending:
        spec = specs.setdefault(r.item_id, {"item": r.item, "pieces": [], "quantity": 0, "reservations": []})
        spec["reservations"].append(r)
        if r.piece_id:
            spec["pieces"].append(r.piece)
        else:
            spec["quantity"] += r.quantity
    lines = []
    for spec in specs.values():
        item = spec["item"]
        if item.kind == ItemKind.TRACKED:
            pieces = list(spec["pieces"])
            if spec["quantity"]:
                out = pieces_out(item)
                taken = {p.pk for p in pieces}
                candidates = [
                    p for p in active_pieces(item).order_by("number") if p.pk not in out and p.pk not in taken
                ]
                # Prefer pieces in good shape.
                candidates.sort(key=lambda p: p.condition in NEEDS_CARE)
                pieces += candidates[: spec["quantity"]]
            lines.append({"item": item, "pieces": pieces})
        else:
            lines.append({"item": item, "quantity": spec["quantity"]})
    loan, warnings = check_out(
        event.organization,
        borrower={"borrower_type": BorrowerType.EVENT, "event": event, "due_date": event.end_date},
        lines=lines,
        note="",
        actor=actor,
    )
    created = list(loan.lines.all())
    for spec in specs.values():
        mine = [line for line in created if line.item_id == spec["item"].pk]
        for r in spec["reservations"]:
            r.loan_line = mine[0] if mine else None
            r.save(update_fields=["loan_line"])
    return loan, warnings


# ---------------------------------------------------------------- stock-take


@transaction.atomic
def start_stocktake(organization, location, actor) -> StockTake:
    st = StockTake.objects.create(organization=organization, location=location, started_by=actor)
    items = Item.objects.filter(organization=organization, is_archived=False)
    if location is not None:
        items = items.filter(location=location)
    lines = []
    for item in items:
        if item.kind == ItemKind.TRACKED:
            out = pieces_out(item)
            for piece in active_pieces(item):
                if piece.pk not in out:
                    lines.append(StockTakeLine(stocktake=st, item=item, piece=piece, expected=1))
        elif item.kind == ItemKind.COUNTED:
            lines.append(StockTakeLine(stocktake=st, item=item, expected=max(item.quantity - quantity_out(item), 0)))
        else:
            lines.append(StockTakeLine(stocktake=st, item=item, expected=item.quantity))
    StockTakeLine.objects.bulk_create(lines)
    return st


@transaction.atomic
def finish_stocktake(st: StockTake, actor) -> dict:
    if st.finished_at:
        raise ValidationError(_("This stock-take is already finished."))
    differences = 0
    for line in st.lines.select_related("item", "piece"):
        if line.counted is None or line.counted == line.expected:
            continue
        differences += 1
        item = Item.objects.select_for_update().get(pk=line.item_id)
        diff = line.counted - line.expected
        note = _("Stock-take: expected %(e)s, counted %(c)s") % {"e": line.expected, "c": line.counted}
        if line.piece_id:
            # Not found: noted in the history; the quartermaster decides whether it's lost.
            log(item, LogKind.ADJUSTED, actor, piece=line.piece, note=note)
            continue
        item.quantity = max(item.quantity + diff, 0)
        item.damaged_quantity = min(item.damaged_quantity, item.quantity)
        item.save(update_fields=["quantity", "damaged_quantity", "updated_at"])
        log(item, LogKind.ADJUSTED, actor, quantity=diff, note=note)
    st.finished_at = timezone.now()
    st.finished_by = actor
    st.save(update_fields=["finished_at", "finished_by", "updated_at"])
    return {"differences": differences}


# ---------------------------------------------------------------- alerts


def overdue_loans(organization):
    today = timezone.localdate()
    ids = open_lines(organization).filter(loan__due_date__lt=today).values_list("loan_id", flat=True)
    return Loan.objects.filter(pk__in=set(ids))
