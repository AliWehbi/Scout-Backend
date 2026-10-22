from datetime import date

from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.attendance.models import AttendanceRecord, AttendanceStatus
from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.customfields import services as custom_field_services
from apps.documents.views import DocumentThrottle, export_response, values_response
from apps.permissions.drf import HasOrgPermission, IsOrgMember, get_permission_service
from apps.units.models import Patrol, UnitMembership

from . import services
from .models import CalendarFeed, Event, EventInvitation, EventStatus, EventType, RespondedAs
from .serializers import (
    EventAttendanceSerializer,
    EventSerializer,
    EventTypeSerializer,
    InviteSerializer,
    RespondSerializer,
    SignSerializer,
    TypeBriefSerializer,
    invitation_row,
)


def _bool_param(value):
    return {"true": True, "1": True, "false": False, "0": False}.get((value or "").lower())


class EventTypeViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EventTypeSerializer
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {
        "create": "settings.manage",
        "update": "settings.manage",
        "partial_update": "settings.manage",
        "destroy": "settings.manage",
    }
    pagination_class = None

    def get_queryset(self):
        return (
            EventType.objects.filter(organization=self.organization)
            .annotate(n_events=Count("events"))
            .order_by("order", "id")
        )

    def perform_create(self, serializer):
        last = EventType.objects.filter(organization=self.organization).order_by("-order").first()
        event_type = serializer.save(organization=self.organization, order=(last.order + 1) if last else 0)
        log_action(self.request, "event_type.created", organization=self.organization, target=event_type)

    def perform_update(self, serializer):
        event_type = serializer.save()
        log_action(self.request, "event_type.updated", organization=self.organization, target=event_type)

    def perform_destroy(self, instance):
        if instance.events.exists():
            raise ValidationError(_("This type is used by events. Archive it instead."))
        log_action(self.request, "event_type.deleted", organization=self.organization, target=instance)
        instance.delete()


class EventViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """
    Events visible with `events.view`. Creating and editing needs `events.manage` for every
    unit of the event (organization-wide events need it organization-wide); check-in needs
    `events.check_in` for one of its units.
    """

    serializer_class = EventSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    @property
    def service(self):
        return get_permission_service(self.request)

    def get_queryset(self):
        qs = services.visible_events(self.service, self.organization)
        params = self.request.query_params
        if params.get("date_from"):
            qs = qs.filter(end_date__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(start_date__lte=params["date_to"])
        if params.get("unit"):
            qs = qs.filter(units=params["unit"])
        if params.get("event_type"):
            qs = qs.filter(event_type_id=params["event_type"])
        mandatory = _bool_param(params.get("mandatory"))
        if mandatory is not None:
            qs = qs.filter(is_mandatory=mandatory)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if self.action == "list":
            ids = list(qs.values_list("pk", flat=True))
            qs = Event.objects.filter(pk__in=ids)
        else:
            qs = Event.objects.filter(pk__in=qs.values("pk"))
        return (
            services.with_counts(qs)
            .select_related("event_type")
            .prefetch_related("units", "leaders")
            .order_by("start_date", "start_time", "id")
        )

    def detail_data(self, event):
        event = self.get_queryset().get(pk=event.pk)
        data = EventSerializer(event, context=self.get_serializer_context()).data
        data["can_manage"] = services.can_manage(self.service, event)
        data["can_check_in"] = services.can_check_in(self.service, event)
        data["leader_names"] = [m.full_name for m in event.leaders.all()]
        membership = get_active_membership(self.request)
        data["closed_at"] = event.closed_at
        data["closed_by_name"] = event.closed_by.get_full_name() or event.closed_by.username if event.closed_by else ""
        data["can_close"] = services.can_close(membership, event)
        data["can_reopen"] = services.can_reopen(membership, event)
        return data

    def retrieve(self, request, *args, **kwargs):
        return Response(self.detail_data(self.get_object()))

    def require_manage(self, event):
        if not services.can_manage(self.service, event):
            raise PermissionDenied(_("You can't manage this event."))

    def create(self, request, *args, **kwargs):
        if not self.service.has_perm_anywhere("events.manage"):
            raise PermissionDenied()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        units = data.pop("units", [])
        leaders = data.pop("leaders", [])
        repeat_until = data.pop("repeat_until", None)
        data.pop("resign_required", None)
        publish = data.pop("publish", False)
        services.require_manage_units(self.service, [u.pk for u in units])
        if publish:
            data["status"] = EventStatus.PUBLISHED
        events = services.create_event(
            organization=self.organization,
            data=data,
            units=units,
            leaders=leaders,
            repeat_until=repeat_until,
            actor=request.user,
        )
        for event in events:
            log_action(request, "event.created", organization=self.organization, target=event)
        body = self.detail_data(events[0])
        body["created_count"] = len(events)
        return Response(body, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        serializer = self.get_serializer(event, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if "units" in data:
            services.require_manage_units(self.service, [u.pk for u in data["units"]])
        resign = data.pop("resign_required", False)
        publish = data.pop("publish", False)
        data.pop("repeat_until", None)
        old_text, old_capacity = event.consent_text, event.capacity
        with transaction.atomic():
            units = data.pop("units", None)
            leaders = data.pop("leaders", None)
            for key, value in data.items():
                setattr(event, key, value)
            if publish and event.status == EventStatus.DRAFT:
                event.status = EventStatus.PUBLISHED
            event.save()
            if units is not None:
                event.units.set(units)
            if leaders is not None:
                event.leaders.set(leaders)
            revoked = services.apply_consent_change(event, old_text=old_text, resign=resign)
            if event.requires_consent:
                event.invitations.filter(include_parents=False).update(include_parents=True)
            if event.capacity != old_capacity:
                services.promote_waitlist(event)
            services.sync_sessions(event)
        changes = {"signatures_revoked": revoked} if revoked else None
        log_action(request, "event.updated", organization=self.organization, target=event, changes=changes)
        return Response(self.detail_data(event))

    def destroy(self, request, *args, **kwargs):
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        if AttendanceRecord.objects.filter(session__event=event).exists():
            raise ValidationError(_("Attendance was taken for this event. Cancel it instead."))
        log_action(request, "event.deleted", organization=self.organization, target=event)
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        if event.status != EventStatus.PUBLISHED:
            event.status = EventStatus.PUBLISHED
            event.save(update_fields=["status", "updated_at"])
            log_action(request, "event.published", organization=self.organization, target=event)
        return Response(self.detail_data(event))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        if event.status != EventStatus.CANCELLED:
            event.status = EventStatus.CANCELLED
            event.save(update_fields=["status", "updated_at"])
            log_action(request, "event.cancelled", organization=self.organization, target=event)
        return Response(self.detail_data(event))

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """Lock the event: nothing about it, its participants or its attendance can change."""
        event = self.get_object()
        if event.is_closed:
            raise ValidationError(_("This event is already closed."))
        if not services.can_close(get_active_membership(request), event):
            raise PermissionDenied(_("Only the event's leaders can close it."))
        event.closed_at = timezone.now()
        event.closed_by = request.user
        event.save(update_fields=["closed_at", "closed_by", "updated_at"])
        log_action(request, "event.closed", organization=self.organization, target=event)
        return Response(self.detail_data(event))

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        event = self.get_object()
        if not event.is_closed:
            raise ValidationError(_("This event isn't closed."))
        if not services.can_reopen(get_active_membership(request), event):
            raise PermissionDenied(_("Only the leader who closed this event, or an admin, can reopen it."))
        event.closed_at = None
        event.closed_by = None
        event.save(update_fields=["closed_at", "closed_by", "updated_at"])
        log_action(request, "event.reopened", organization=self.organization, target=event)
        return Response(self.detail_data(event))

    @action(detail=True, methods=["get"], throttle_classes=[DocumentThrottle])
    def export(self, request, pk=None):
        """The event as a Word document, filled into the organization's template."""
        return export_response(request, event=self.get_object(), service=self.service, organization=self.organization)

    @action(detail=True, methods=["get"], url_path="document-values")
    def document_values(self, request, pk=None):
        """The custom fields the chosen template uses, to check before exporting."""
        return values_response(request, event=self.get_object(), service=self.service, organization=self.organization)

    # ------------------------------------------------------------ invitations

    def invitation_queryset(self, event):
        unit_ids = list(event.units.values_list("pk", flat=True))
        memberships = UnitMembership.objects.select_related("unit", "patrol")
        if unit_ids:
            memberships = memberships.filter(unit_id__in=unit_ids)
        return event.invitations.select_related("member", "responded_by", "event").prefetch_related(
            "signatures", Prefetch("member__unit_memberships", queryset=memberships)
        )

    def invitation_rows(self, event):
        invitations = list(self.invitation_queryset(event))
        waiting = sorted((i for i in invitations if i.waitlisted), key=lambda i: (i.responded_at, i.pk))
        positions = {inv.pk: n for n, inv in enumerate(waiting, start=1)}
        return [invitation_row(inv, positions.get(inv.pk)) for inv in invitations]

    @action(detail=True, methods=["get"])
    def invitations(self, request, pk=None):
        return Response(self.invitation_rows(self.get_object()))

    @action(detail=True, methods=["post"])
    def invite(self, request, pk=None):
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        if event.status == EventStatus.CANCELLED:
            raise ValidationError(_("This event was cancelled."))
        serializer = InviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        org = self.organization
        patrol_units = Patrol.objects.filter(organization=org, pk__in=data["patrols"]).values_list("unit_id", flat=True)
        for unit_id in set(data["units"]) | set(patrol_units):
            if not self.service.has_perm("events.manage", unit_id):
                raise PermissionDenied(_("You can't invite members of this unit."))
        members = services.members_for_invite(
            org, unit_ids=data["units"], patrol_ids=data["patrols"], member_ids=data["members"]
        )
        added = services.invite(event, members, include_parents=data["include_parents"], actor=request.user)
        log_action(
            request,
            "event.invited",
            organization=org,
            target=event,
            changes={"added": added, "include_parents": data["include_parents"] or event.requires_consent},
        )
        return Response({"added": added, "invitations": self.invitation_rows(event)})

    def get_invitation(self, event, invitation_id):
        return get_object_or_404(EventInvitation, event=event, pk=invitation_id)

    @action(detail=True, methods=["delete"], url_path=r"invitations/(?P<invitation_id>\d+)")
    def uninvite(self, request, pk=None, invitation_id=None):
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        invitation = self.get_invitation(event, invitation_id)
        invitation.delete()
        services.promote_waitlist(event)
        log_action(request, "event.uninvited", organization=self.organization, target=event)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path=r"invitations/(?P<invitation_id>\d+)/respond")
    def respond_for(self, request, pk=None, invitation_id=None):
        """A leader records a reply on someone's behalf."""
        event = self.get_object()
        self.require_manage(event)
        services.require_open(event)
        invitation = self.get_invitation(event, invitation_id)
        serializer = RespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.respond(
            invitation, serializer.validated_data["response"], user=request.user, responded_as=RespondedAs.LEADER
        )
        return Response(self.invitation_rows(event))

    # ------------------------------------------------------------ check-in (once per day)

    def attendance_payload(self, event, day):
        session, roster = services.event_roster(event, day)
        counts = {s: 0 for s in AttendanceStatus.values}
        for row in roster:
            if row["status"]:
                counts[row["status"]] += 1
        return {
            "event": event.pk,
            "date": day.isoformat(),
            "days": [d.isoformat() for d in event.days],
            "session": session.pk if session else None,
            "title": event.title,
            "is_mandatory": event.is_mandatory,
            "counts": counts,
            "roster": roster,
            "can_take": services.can_check_in(self.service, event) and not event.is_closed,
        }

    @action(detail=True, methods=["get", "put"])
    def attendance(self, request, pk=None):
        event = self.get_object()
        if request.method == "GET":
            day = request.query_params.get("date")
            try:
                day = date.fromisoformat(day) if day else None
            except ValueError:
                raise ValidationError({"date": [_("Invalid date.")]}) from None
            if day is None or day not in event.days:
                today = date.today()
                day = today if today in event.days else event.start_date
            return Response(self.attendance_payload(event, day))

        if not services.can_check_in(self.service, event):
            raise PermissionDenied(_("You can't take attendance for this event."))
        services.require_open(event)
        serializer = EventAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        day = serializer.validated_data["date"]
        allowed = set(event.invitations.values_list("member_id", flat=True))
        with transaction.atomic():
            session = services.ensure_session(event, day, request.user)
            allowed |= set(session.records.values_list("member_id", flat=True))
            for row in serializer.validated_data["records"]:
                if row["member"] not in allowed:
                    raise ValidationError({"records": [_("Member %(id)s isn't invited.") % {"id": row["member"]}]})
                if row["status"] is None:
                    AttendanceRecord.objects.filter(session=session, member_id=row["member"]).delete()
                else:
                    note = row["note"] if row["status"] == AttendanceStatus.EXCUSED else ""
                    AttendanceRecord.objects.update_or_create(
                        session=session, member_id=row["member"], defaults={"status": row["status"], "note": note}
                    )
        log_action(
            request, "event.attendance_taken", organization=self.organization, target=event, changes={"date": str(day)}
        )
        return Response(self.attendance_payload(event, day))


# ---------------------------------------------------------------- members & parents


def family_fields(organization):
    """Event custom fields the organization shows to families."""
    return [f for f in custom_field_services.active_fields(organization, "event") if f.visible_to_parents]


def family_values(event: Event, fields) -> list[dict]:
    rows = []
    for field in fields:
        if not field.applies(event):
            continue
        value, _source = custom_field_services.value_or_default(field, event)
        if value in custom_field_services.EMPTY:
            continue
        rows.append(
            {
                "key": field.key,
                "label_en": field.label_en,
                "label_ar": field.label_ar or field.label_en,
                "value_en": custom_field_services.display(field, value, "en"),
                "value_ar": custom_field_services.display(field, value, "ar"),
            }
        )
    return rows


def my_event_payload(event: Event, invitations: list[EventInvitation], membership, fields=None) -> dict:
    me, guardian, children = services.my_people(membership)
    people = []
    for inv in invitations:
        as_ = services.responder_role(inv, membership)
        position = services.waitlist_position(inv)
        sig = inv.valid_signature
        people.append(
            {
                "invitation": inv.pk,
                "member": inv.member_id,
                "full_name": inv.member.full_name,
                "name_ar": inv.member.name_ar,
                "first_name": inv.member.first_name,
                "is_me": me is not None and inv.member_id == me.pk,
                "response": inv.response,
                "waitlisted": inv.waitlisted,
                "waitlist_position": position,
                "can_reply": as_ is not None and event.replies_open,
                "needs_signature": services.needs_signature(inv),
                "can_sign": guardian is not None
                and inv.member_id in children
                and services.needs_signature(inv)
                and not event.is_closed,
                "signature": {"name": sig.signer_name, "signed_at": sig.signed_at} if sig else None,
            }
        )
    return {
        "id": event.pk,
        "title": event.title,
        "description": event.description,
        "type": TypeBriefSerializer(event.event_type).data,
        "start_date": event.start_date,
        "start_time": event.start_time,
        "end_date": event.end_date,
        "end_time": event.end_time,
        "location": event.location,
        "is_mandatory": event.is_mandatory,
        "cost": event.cost,
        "reply_deadline": event.reply_deadline,
        "replies_open": event.replies_open,
        "requires_consent": event.requires_consent,
        "consent_text": event.consent_text,
        "status": event.status,
        "closed": event.is_closed,
        "custom_fields": family_values(event, family_fields(event.organization) if fields is None else fields),
        "people": people,
    }


class MyEventsView(APIView):
    """Events the current login or their children are invited to."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        membership = get_active_membership(request)
        today = date.today()
        qs = services.family_invitations(membership)
        if request.query_params.get("past") in ("1", "true"):
            qs = qs.filter(event__end_date__lt=today).order_by("-event__start_date")
        else:
            qs = qs.filter(event__end_date__gte=today).order_by("event__start_date", "event__start_time")
        grouped: dict[int, tuple[Event, list]] = {}
        for inv in qs[:300]:
            grouped.setdefault(inv.event_id, (inv.event, []))[1].append(inv)
        fields = family_fields(membership.organization)
        return Response([my_event_payload(e, invs, membership, fields) for e, invs in grouped.values()])


class MyInvitationMixin:
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get_invitation(self, request, pk):
        membership = get_active_membership(request)
        invitation = services.family_invitations(membership).filter(pk=pk).first()
        if invitation is None:
            raise PermissionDenied()
        return membership, invitation

    def payload(self, invitation, membership):
        event = invitation.event
        invitations = [i for i in services.family_invitations(membership).filter(event=event)]
        return my_event_payload(event, invitations, membership)


class MyInvitationRespondView(MyInvitationMixin, APIView):
    def post(self, request, pk):
        membership, invitation = self.get_invitation(request, pk)
        role = services.responder_role(invitation, membership)
        if role is None:
            raise PermissionDenied()
        services.require_open(invitation.event)
        if not invitation.event.replies_open:
            raise ValidationError(_("Replies are closed for this event."))
        serializer = RespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.respond(invitation, serializer.validated_data["response"], user=request.user, responded_as=role)
        return Response(self.payload(invitation, membership))


class MyInvitationSignView(MyInvitationMixin, APIView):
    def post(self, request, pk):
        membership, invitation = self.get_invitation(request, pk)
        _me, guardian, children = services.my_people(membership)
        if guardian is None or invitation.member_id not in children:
            raise PermissionDenied(_("Only a parent or guardian can sign."))
        services.require_open(invitation.event)
        serializer = SignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.sign(invitation, guardian=guardian, user=request.user, name=serializer.validated_data["name"])
        log_action(request, "event.consent_signed", organization=membership.organization, target=invitation.event)
        invitation = EventInvitation.objects.prefetch_related("signatures").get(pk=invitation.pk)
        return Response(self.payload(invitation, membership))


class MyCalendarView(APIView):
    """The private iCal link of the current login. POST makes a new link (the old one stops working)."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        feed, _created = CalendarFeed.objects.get_or_create(membership=get_active_membership(request))
        return Response({"token": feed.token})

    def post(self, request):
        membership = get_active_membership(request)
        CalendarFeed.objects.filter(membership=membership).delete()
        feed = CalendarFeed.objects.create(membership=membership)
        return Response({"token": feed.token})


class CalendarFeedView(APIView):
    """Public iCal feed; the unguessable token is the credential."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request, token):
        feed = CalendarFeed.objects.select_related("membership__organization").filter(token=token).first()
        if feed is None or not feed.membership.is_active or not feed.membership.organization.is_active:
            return HttpResponse(status=404)
        response = HttpResponse(services.build_ics(feed.membership), content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = 'inline; filename="events.ics"'
        return response
