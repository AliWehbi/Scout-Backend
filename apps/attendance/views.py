from datetime import date

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.members.models import Member, MemberStatus
from apps.permissions.drf import IsOrgMember, get_permission_service
from apps.units.models import UnitMembership

from .models import AttendanceRecord, AttendanceStatus, Session
from .serializers import RecordListSerializer, SessionSerializer


def rate(records, excused_absent: bool = False) -> int | None:
    """Percent present. Excused absences are left out, or counted as absent when `excused_absent`."""
    counted = [r for r in records if excused_absent or r.status != AttendanceStatus.EXCUSED]
    if not counted:
        return None
    return round(100 * sum(r.status == AttendanceStatus.PRESENT for r in counted) / len(counted))


def visible_unit_ids(service) -> set[int] | None:
    """Units whose attendance this login can see (`None` = every unit)."""
    ids = set()
    for codename in ("members.view", "events.check_in"):
        scope = service.unit_ids(codename)
        if scope is None and service.has_perm(codename):
            return None
        ids |= scope or set()
    return ids


# Events with no units are organization-wide: they count for every unit.
ORG_WIDE_EVENT = Q(event__isnull=False, event__units__isnull=True)


def scoped_sessions(organization, visible_ids):
    """Standalone sessions and event check-ins in the visible units."""
    qs = Session.objects.filter(organization=organization)
    if visible_ids is None:
        return qs
    qs = qs.filter(Q(unit_id__in=visible_ids) | Q(event__units__in=visible_ids) | ORG_WIDE_EVENT)
    return Session.objects.filter(pk__in=qs.values("pk"))


def unit_sessions(organization, visible_ids, unit_ids):
    qs = scoped_sessions(organization, visible_ids).filter(
        Q(unit_id__in=unit_ids) | Q(event__units__in=unit_ids) | ORG_WIDE_EVENT
    )
    return Session.objects.filter(pk__in=qs.values("pk"))


def requested_units(params, visible_ids) -> set[int]:
    """The `units` query param (comma separated; `unit` also accepted), checked against what's visible."""
    raw = params.get("units") or params.get("unit") or ""
    try:
        ids = {int(x) for x in raw.split(",") if x.strip()}
    except ValueError:
        raise ValidationError({"units": [_("Invalid unit list.")]}) from None
    if visible_ids is not None and not ids <= visible_ids:
        raise PermissionDenied()
    return ids


def unit_member_ids(unit_ids) -> set[int] | None:
    if unit_ids is None:
        return None
    return set(UnitMembership.objects.filter(unit_id__in=unit_ids).values_list("member_id", flat=True))


def excused_absent(params) -> bool:
    """`?excused=absent`: a leader's strict view, where an excused absence still counts as missed."""
    return params.get("excused") == "absent"


def counted_records(session, member_ids):
    """
    A session's records, limited to `member_ids` for event check-ins: an event can span
    several units (or the whole organization), and only the members in scope count.
    """
    records = session.records.all()
    if member_ids is None or not session.event_id:
        return list(records)
    return [r for r in records if r.member_id in member_ids]


class SessionViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """
    Unit meetings and their attendance. Seeing a unit's sessions needs `members.view` or
    `events.check_in` for that unit; taking attendance needs `events.check_in`.
    """

    serializer_class = SessionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]
    filterset_fields = {"unit": ["exact", "in"], "date": ["gte", "lte"]}

    @property
    def service(self):
        return get_permission_service(self.request)

    def visible_unit_ids(self):
        return visible_unit_ids(self.service)

    def require_check_in(self, unit):
        if not self.service.has_perm("events.check_in", unit):
            raise PermissionDenied(_("You can't take attendance for this unit."))

    def get_queryset(self):
        # Event check-ins live on the event; this endpoint is for standalone sessions.
        qs = (
            Session.objects.filter(organization=self.organization, event__isnull=True)
            .select_related("unit")
            .prefetch_related("records")
        )
        ids = self.visible_unit_ids()
        return qs if ids is None else qs.filter(unit_id__in=ids)

    def perform_create(self, serializer):
        self.require_check_in(serializer.validated_data["unit"])
        session = serializer.save(organization=self.organization, created_by=self.request.user)
        log_action(self.request, "session.created", organization=self.organization, target=session)

    def perform_update(self, serializer):
        self.require_check_in(serializer.instance.unit)
        if "unit" in serializer.validated_data:
            self.require_check_in(serializer.validated_data["unit"])
        serializer.save()

    def perform_destroy(self, instance):
        self.require_check_in(instance.unit)
        log_action(self.request, "session.deleted", organization=self.organization, target=instance)
        instance.delete()

    def roster(self, session):
        members = (
            Member.objects.filter(organization=self.organization, unit_memberships__unit=session.unit)
            .exclude(status=MemberStatus.ALUMNI)
            .prefetch_related(
                Prefetch(
                    "unit_memberships",
                    queryset=UnitMembership.objects.filter(unit=session.unit).select_related("patrol"),
                )
            )
            .distinct()
            .order_by("first_name", "last_name")
        )
        records = {r.member_id: r for r in session.records.all()}
        extra = Member.objects.filter(pk__in=[mid for mid in records if mid not in {m.pk for m in members}])
        rows = []
        for m in list(members) + list(extra):
            um = next(iter(m.unit_memberships.all()), None)
            record = records.get(m.pk)
            rows.append(
                {
                    "member": m.pk,
                    "full_name": m.full_name,
                    "name_ar": m.name_ar,
                    "position": um.position if um else "",
                    "patrol": um.patrol.name if um and um.patrol else "",
                    "status": record.status if record else None,
                    "note": record.note if record else "",
                }
            )
        return rows

    def retrieve(self, request, *args, **kwargs):
        session = self.get_object()
        data = SessionSerializer(session, context=self.get_serializer_context()).data
        data["roster"] = self.roster(session)
        data["can_take"] = self.service.has_perm("events.check_in", session.unit)
        return Response(data)

    @action(detail=True, methods=["put"])
    def records(self, request, pk=None):
        session = self.get_object()
        self.require_check_in(session.unit)
        serializer = RecordListSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        allowed = set(
            Member.objects.filter(organization=self.organization, unit_memberships__unit=session.unit).values_list(
                "pk", flat=True
            )
        ) | set(session.records.values_list("member_id", flat=True))
        with transaction.atomic():
            for row in serializer.validated_data["records"]:
                if row["member"] not in allowed:
                    raise ValidationError(
                        {"records": [_("Member %(id)s is not in this unit.") % {"id": row["member"]}]}
                    )
                if row["status"] is None:
                    AttendanceRecord.objects.filter(session=session, member_id=row["member"]).delete()
                else:
                    # The note is the reason for an excused absence.
                    note = row["note"] if row["status"] == AttendanceStatus.EXCUSED else ""
                    AttendanceRecord.objects.update_or_create(
                        session=session,
                        member_id=row["member"],
                        defaults={"status": row["status"], "note": note},
                    )
        log_action(request, "session.attendance_taken", organization=self.organization, target=session)
        session = self.get_queryset().get(pk=session.pk)
        data = SessionSerializer(session, context=self.get_serializer_context()).data
        data["roster"] = self.roster(session)
        data["can_take"] = True
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def trend(self, request):
        """Attendance rate of the last 8 sessions of one or more units."""
        visible = self.visible_unit_ids()
        unit_ids = requested_units(request.query_params, visible)
        sessions = (
            list(
                unit_sessions(self.organization, visible, unit_ids)
                .filter(date__lte=date.today())
                .prefetch_related("records")
                .order_by("-date", "-id")[:8]
            )
            if unit_ids
            else []
        )
        members = unit_member_ids(unit_ids)
        strict = excused_absent(request.query_params)
        points = [
            {"id": s.pk, "title": s.title, "date": s.date, "rate": rate(counted_records(s, members), strict)}
            for s in reversed(sessions)
        ]
        rates = [p["rate"] for p in points if p["rate"] is not None]
        return Response({"points": points, "average": round(sum(rates) / len(rates)) if rates else None})


class AttendanceReportView(APIView):
    """
    Attendance per member across standalone sessions and event check-ins (one per event day).
    Filters: units (comma separated), date_from, date_to, mandatory (true/false), source (events/sessions).
    """

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        service = get_permission_service(request)
        if not (service.has_perm_anywhere("members.view") or service.has_perm_anywhere("events.check_in")):
            raise PermissionDenied()
        organization = get_active_membership(request).organization
        visible = visible_unit_ids(service)
        params = request.query_params
        strict = excused_absent(params)
        unit_ids = requested_units(params, visible)
        base = unit_sessions(organization, visible, unit_ids) if unit_ids else scoped_sessions(organization, visible)
        base = base.filter(date__lte=date.today())
        if params.get("date_from"):
            base = base.filter(date__gte=params["date_from"])
        if params.get("date_to"):
            base = base.filter(date__lte=params["date_to"])
        source = params.get("source")
        if source == "events":
            base = base.filter(event__isnull=False)
        elif source == "sessions":
            base = base.filter(event__isnull=True)
        mandatory = {"true": True, "false": False}.get(params.get("mandatory", ""))
        sessions = base if mandatory is None else base.filter(is_mandatory=mandatory)
        hidden = 0 if mandatory is None else base.filter(is_mandatory=not mandatory).count()
        sessions = list(sessions.prefetch_related("records").order_by("-date", "-id"))

        in_scope = unit_member_ids(unit_ids or visible)
        records = {s.pk: counted_records(s, in_scope) for s in sessions}
        per_member: dict[int, dict] = {}
        for s in sessions:
            for r in records[s.pk]:
                row = per_member.setdefault(r.member_id, {k: 0 for k in AttendanceStatus.values})
                row[r.status] += 1
        member_qs = Member.objects.filter(organization=organization)
        if unit_ids:
            member_qs = member_qs.filter(
                Q(pk__in=per_member) | Q(unit_memberships__unit_id__in=unit_ids, status=MemberStatus.ACTIVE)
            ).distinct()
        else:
            member_qs = member_qs.filter(pk__in=per_member)
        members = []
        for m in member_qs.order_by("first_name", "last_name"):
            c = per_member.get(m.pk, {k: 0 for k in AttendanceStatus.values})
            counted = c["present"] + c["absent"] + (c["excused"] if strict else 0)
            members.append(
                {
                    "member": m.pk,
                    "full_name": m.full_name,
                    "name_ar": m.name_ar,
                    **c,
                    "rate": round(100 * c["present"] / counted) if counted else None,
                }
            )
        present = sum(m["present"] for m in members)
        absent = sum(m["absent"] + (m["excused"] if strict else 0) for m in members)
        occasions = [
            {
                "id": s.pk,
                "event": s.event_id,
                "title": s.title,
                "date": s.date,
                "source": "event" if s.event_id else "session",
                "is_mandatory": s.is_mandatory,
                "rate": rate(records[s.pk], strict),
            }
            for s in sessions
        ]
        return Response(
            {
                "summary": {
                    "held": len(sessions),
                    "events": sum(1 for s in sessions if s.event_id),
                    "sessions": sum(1 for s in sessions if not s.event_id),
                    "average": round(100 * present / (present + absent)) if present + absent else None,
                    "below_75": sum(1 for m in members if m["rate"] is not None and m["rate"] < 75),
                    "excused": sum(m["excused"] for m in members),
                    "hidden": hidden,
                },
                "members": members,
                "occasions": occasions[:100],
            }
        )
