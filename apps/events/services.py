"""
Business rules for events: who can see and manage them, invitations, replies (with a
waitlist when an event is full), parent signatures, repeating events, per-day check-in
sessions and the iCal feed.
"""

import uuid
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.attendance.models import Session
from apps.members.models import Guardian, GuardianLink, Member, MemberStatus
from apps.units.models import UnitMembership

from .models import (
    ConsentSignature,
    Event,
    EventInvitation,
    EventStatus,
    EventType,
    RespondedAs,
    Response,
    TypeColor,
)

MAX_REPEATS = 52

DEFAULT_TYPES = (
    {
        "name_en": "Meeting",
        "name_ar": "اجتماع",
        "color": TypeColor.PRIMARY,
        "icon": "groups",
        "default_mandatory": True,
    },
    {
        "name_en": "Activity",
        "name_ar": "نشاط",
        "color": TypeColor.SECONDARY,
        "icon": "hiking",
        "default_mandatory": False,
    },
)


def create_default_event_types(organization) -> None:
    for order, spec in enumerate(DEFAULT_TYPES):
        EventType.objects.get_or_create(
            organization=organization, name_en=spec["name_en"], defaults={**spec, "order": order}
        )


# ---------------------------------------------------------------- visibility & rights


def visible_events(service, organization) -> QuerySet:
    """Events a leader can see with `events.view` (organization-wide events need it anywhere)."""
    qs = Event.objects.filter(organization=organization)
    if not service.has_perm_anywhere("events.view"):
        return qs.none()
    ids = service.unit_ids("events.view")
    if ids is None:
        return qs
    return qs.filter(Q(units__isnull=True) | Q(units__in=ids)).distinct()


def _covers(service, codename: str, unit_ids) -> bool:
    """True if the permission applies to every unit given (organization-wide when there are none)."""
    unit_ids = list(unit_ids)
    if not unit_ids:
        return service.has_perm(codename)
    return all(service.has_perm(codename, u) for u in unit_ids)


def can_manage(service, event: Event) -> bool:
    return _covers(service, "events.manage", event.units.values_list("pk", flat=True))


def require_manage_units(service, unit_ids) -> None:
    if not _covers(service, "events.manage", unit_ids):
        raise PermissionDenied(_("You can't manage events for these units."))


def can_check_in(service, event: Event) -> bool:
    unit_ids = list(event.units.values_list("pk", flat=True))
    if not unit_ids:
        return service.has_perm("events.check_in")
    return any(service.has_perm("events.check_in", u) for u in unit_ids)


# ---------------------------------------------------------------- closing


def require_open(event: Event) -> None:
    """Every change to an event, its participants or its attendance stops while it is closed."""
    if event.is_closed:
        raise ValidationError(_("This event is closed. Reopen it to make changes."))


def is_org_admin(membership) -> bool:
    return membership.is_owner or membership.role_assignments.filter(role__key="org_admin", unit__isnull=True).exists()


def can_close(membership, event: Event) -> bool:
    """Only the event's own leaders close it."""
    if event.is_closed or membership is None:
        return False
    return event.leaders.filter(membership=membership).exists()


def can_reopen(membership, event: Event) -> bool:
    """The leader who closed it, or an org admin when that person isn't available."""
    if not event.is_closed or membership is None:
        return False
    return event.closed_by_id == membership.user_id or is_org_admin(membership)


def with_counts(qs: QuerySet) -> QuerySet:
    going = Q(invitations__response=Response.GOING, invitations__waitlisted=False)
    return qs.annotate(
        n_invited=Count("invitations", distinct=True),
        n_going=Count("invitations", filter=going, distinct=True),
        n_maybe=Count("invitations", filter=Q(invitations__response=Response.MAYBE), distinct=True),
        n_not_going=Count("invitations", filter=Q(invitations__response=Response.NOT_GOING), distinct=True),
        n_no_reply=Count("invitations", filter=Q(invitations__response=""), distinct=True),
        n_waitlist=Count("invitations", filter=Q(invitations__waitlisted=True), distinct=True),
        n_signed=Count(
            "invitations",
            filter=going & Q(invitations__signatures__revoked_at__isnull=True, invitations__signatures__isnull=False),
            distinct=True,
        ),
    )


# ---------------------------------------------------------------- creating & editing


@transaction.atomic
def create_event(*, organization, data: dict, units, leaders, repeat_until: date | None, actor) -> list[Event]:
    """Create an event, or a weekly series when `repeat_until` is given."""
    starts = [data["start_date"]]
    if repeat_until:
        if repeat_until < data["start_date"]:
            raise ValidationError({"repeat_until": [_("Must be on or after the start date.")]})
        while len(starts) < MAX_REPEATS and starts[-1] + timedelta(weeks=1) <= repeat_until:
            starts.append(starts[-1] + timedelta(weeks=1))
    length = data["end_date"] - data["start_date"]
    series = uuid.uuid4() if len(starts) > 1 else None
    events = []
    deadline = data.get("reply_deadline")
    for start in starts:
        shift = start - data["start_date"]
        event = Event.objects.create(
            organization=organization,
            created_by=actor,
            series=series,
            **{
                **data,
                "start_date": start,
                "end_date": start + length,
                # Each week keeps the same reply window, e.g. "reply by the Friday before".
                "reply_deadline": deadline + shift if deadline else None,
            },
        )
        event.units.set(units)
        event.leaders.set(leaders)
        events.append(event)
    return events


def apply_consent_change(event: Event, *, old_text: str, resign: bool) -> int:
    """After the consent text changed: revoke signatures when the leader asked parents to sign again."""
    if not resign or event.consent_text == old_text:
        return 0
    return ConsentSignature.objects.filter(invitation__event=event, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def sync_sessions(event: Event) -> None:
    """Keep check-in sessions in line with the event (title, mandatory flag, days)."""
    Session.objects.filter(event=event).update(title=event.title, is_mandatory=event.is_mandatory)
    Session.objects.filter(event=event).exclude(date__in=event.days).filter(records__isnull=True).delete()


# ---------------------------------------------------------------- invitations & replies


def members_for_invite(organization, *, unit_ids=(), patrol_ids=(), member_ids=()) -> QuerySet:
    q = Q(pk__in=list(member_ids))
    if unit_ids:
        q |= Q(unit_memberships__unit_id__in=list(unit_ids))
    if patrol_ids:
        q |= Q(unit_memberships__patrol_id__in=list(patrol_ids))
    return Member.objects.filter(organization=organization).filter(q).exclude(status=MemberStatus.ALUMNI).distinct()


@transaction.atomic
def invite(event: Event, members, *, include_parents: bool, actor) -> int:
    """Invite members (already-invited ones are skipped). A signature requirement always includes parents."""
    include_parents = include_parents or event.requires_consent
    existing = set(event.invitations.values_list("member_id", flat=True))
    new = [
        EventInvitation(event=event, member=m, include_parents=include_parents, invited_by=actor)
        for m in members
        if m.pk not in existing
    ]
    EventInvitation.objects.bulk_create(new)
    if event.requires_consent:
        event.invitations.filter(include_parents=False).update(include_parents=True)
    return len(new)


def going_count(event: Event, exclude=None) -> int:
    qs = event.invitations.filter(response=Response.GOING, waitlisted=False)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.count()


def promote_waitlist(event: Event) -> list[EventInvitation]:
    """Move waitlisted replies up, first come first served, while there is room."""
    promoted = []
    if event.capacity is None:
        waiting = list(event.invitations.filter(waitlisted=True))
    else:
        free = event.capacity - going_count(event)
        waiting = list(event.invitations.filter(waitlisted=True).order_by("responded_at", "id")[: max(free, 0)])
    for inv in waiting:
        inv.waitlisted = False
        inv.save(update_fields=["waitlisted"])
        promoted.append(inv)
    return promoted


@transaction.atomic
def respond(invitation: EventInvitation, response: str, *, user, responded_as: str) -> EventInvitation:
    event = Event.objects.select_for_update().get(pk=invitation.event_id)
    if event.status == EventStatus.CANCELLED:
        raise ValidationError(_("This event was cancelled."))
    invitation.refresh_from_db()
    was_going = invitation.response == Response.GOING and not invitation.waitlisted
    if response == invitation.response:
        return invitation
    invitation.response = response
    invitation.responded_at = timezone.now()
    invitation.responded_by = user
    invitation.responded_as = responded_as
    invitation.waitlisted = (
        response == Response.GOING
        and event.capacity is not None
        and going_count(event, exclude=invitation) >= event.capacity
    )
    invitation.save()
    if was_going and response != Response.GOING:
        promote_waitlist(event)
    return invitation


def waitlist_position(invitation: EventInvitation) -> int | None:
    if not invitation.waitlisted:
        return None
    ahead = invitation.event.invitations.filter(waitlisted=True).filter(
        Q(responded_at__lt=invitation.responded_at) | Q(responded_at=invitation.responded_at, pk__lt=invitation.pk)
    )
    return ahead.count() + 1


def needs_signature(invitation: EventInvitation) -> bool:
    return (
        invitation.event.requires_consent
        and invitation.response == Response.GOING
        and invitation.valid_signature is None
    )


@transaction.atomic
def sign(invitation: EventInvitation, *, guardian: Guardian, user, name: str) -> ConsentSignature:
    event = invitation.event
    if not event.requires_consent:
        raise ValidationError(_("This event doesn't need a signature."))
    if event.status != EventStatus.PUBLISHED:
        raise ValidationError(_("This event isn't open."))
    if invitation.valid_signature is not None:
        raise ValidationError(_("Already signed."))
    name = " ".join(name.split())
    if len(name) < 3:
        raise ValidationError({"name": [_("Type your full name.")]})
    return ConsentSignature.objects.create(
        invitation=invitation, guardian=guardian, signed_by=user, signer_name=name, text=event.consent_text
    )


# ---------------------------------------------------------------- families


def my_people(membership) -> tuple[Member | None, Guardian | None, list[int]]:
    """The login's own member record, guardian record and children's member ids."""
    me = Member.objects.filter(membership=membership).first()
    guardian = Guardian.objects.filter(membership=membership).first()
    children = (
        list(GuardianLink.objects.filter(guardian=guardian).values_list("member_id", flat=True)) if guardian else []
    )
    return me, guardian, children


def family_invitations(membership) -> QuerySet:
    """Invitations to published events that this login may see and answer."""
    me, guardian, children = my_people(membership)
    q = Q(pk__in=[])
    if me is not None:
        q |= Q(member=me)
    if children:
        q |= Q(member_id__in=children) & (Q(include_parents=True) | Q(event__requires_consent=True))
    return (
        EventInvitation.objects.filter(q, event__organization=membership.organization)
        .exclude(event__status=EventStatus.DRAFT)
        .select_related("event", "event__event_type", "member")
        .prefetch_related("signatures")
    )


def responder_role(invitation: EventInvitation, membership) -> str | None:
    """How this login may answer an invitation: as the member, as a parent, or not at all."""
    me, guardian, children = my_people(membership)
    if me is not None and invitation.member_id == me.pk:
        return RespondedAs.SELF
    if invitation.member_id in children and (invitation.include_parents or invitation.event.requires_consent):
        return RespondedAs.PARENT
    return None


def pending_actions(membership) -> int:
    """Replies and signatures this login still owes for upcoming events."""
    today = date.today()
    count = 0
    _me, guardian, children = my_people(membership)
    for inv in family_invitations(membership).filter(event__end_date__gte=today, event__status=EventStatus.PUBLISHED):
        if inv.response == "" and inv.event.replies_open:
            count += 1
        if guardian and inv.member_id in children and needs_signature(inv):
            count += 1
    return count


# ---------------------------------------------------------------- check-in


def event_roster(event: Event, day: date) -> tuple[Session | None, list[dict]]:
    session = Session.objects.filter(event=event, date=day).prefetch_related("records").first()
    records = {r.member_id: r for r in session.records.all()} if session else {}
    invitations = list(event.invitations.select_related("member").prefetch_related("signatures"))
    unit_ids = list(event.units.values_list("pk", flat=True))
    memberships = {
        um.member_id: um
        for um in UnitMembership.objects.filter(member__in=[i.member_id for i in invitations]).select_related("patrol")
        if not unit_ids or um.unit_id in unit_ids
    }
    rows = []
    for inv in invitations:
        m = inv.member
        um = memberships.get(m.pk)
        record = records.pop(m.pk, None)
        rows.append(
            {
                "member": m.pk,
                "full_name": m.full_name,
                "name_ar": m.name_ar,
                "position": um.position if um else "",
                "patrol": um.patrol.name if um and um.patrol else "",
                "response": inv.response,
                "waitlisted": inv.waitlisted,
                "missing_signature": needs_signature(inv),
                "status": record.status if record else None,
                "note": record.note if record else "",
            }
        )
    for record in records.values():  # marked earlier, invitation removed since
        m = record.member
        rows.append(
            {
                "member": m.pk,
                "full_name": m.full_name,
                "name_ar": m.name_ar,
                "position": "",
                "patrol": "",
                "response": "",
                "waitlisted": False,
                "missing_signature": False,
                "status": record.status,
                "note": record.note,
            }
        )
    return session, rows


def ensure_session(event: Event, day: date, actor) -> Session:
    if day not in event.days:
        raise ValidationError({"date": [_("This day isn't part of the event.")]})
    session, _created = Session.objects.get_or_create(
        event=event,
        date=day,
        defaults={
            "organization": event.organization,
            "title": event.title,
            "start_time": event.start_time if day == event.start_date else None,
            "end_time": event.end_time if day == event.end_date else None,
            "location": event.location,
            "is_mandatory": event.is_mandatory,
            "created_by": actor,
        },
    )
    return session


# ---------------------------------------------------------------- iCal


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    out, line = [], line
    while len(line.encode()) > 74:
        cut = 74
        while len(line[:cut].encode()) > 74:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def feed_events(membership) -> QuerySet:
    from apps.permissions.service import PermissionService

    service = PermissionService(membership)
    ids = set(visible_events(service, membership.organization).values_list("pk", flat=True))
    ids |= set(family_invitations(membership).values_list("event_id", flat=True))
    since = date.today() - timedelta(days=365)
    return (
        Event.objects.filter(pk__in=ids, end_date__gte=since)
        .exclude(status=EventStatus.DRAFT)
        .select_related("event_type")
    )


def build_ics(membership) -> str:
    org = membership.organization
    tz = org.timezone or "UTC"
    stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Scout Dashboard//Events//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(org.name)}",
    ]
    for e in feed_events(membership):
        lines += ["BEGIN:VEVENT", f"UID:event-{e.pk}@{org.pk}", f"DTSTAMP:{stamp}"]
        if e.start_time:
            start = datetime.combine(e.start_date, e.start_time)
            end = datetime.combine(e.end_date, e.end_time or e.start_time)
            if end <= start:
                end = start + timedelta(hours=1)
            lines += [f"DTSTART;TZID={tz}:{start:%Y%m%dT%H%M%S}", f"DTEND;TZID={tz}:{end:%Y%m%dT%H%M%S}"]
        else:
            lines += [
                f"DTSTART;VALUE=DATE:{e.start_date:%Y%m%d}",
                f"DTEND;VALUE=DATE:{e.end_date + timedelta(days=1):%Y%m%d}",
            ]
        lines.append(f"SUMMARY:{_ics_escape(e.title)}")
        if e.location:
            lines.append(f"LOCATION:{_ics_escape(e.location)}")
        if e.description:
            lines.append(f"DESCRIPTION:{_ics_escape(e.description)}")
        if e.status == EventStatus.CANCELLED:
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"
