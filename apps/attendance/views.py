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


def rate(records) -> int | None:
    counted = [r for r in records if r.status != AttendanceStatus.EXCUSED]
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


def scoped_sessions(organization, visible_ids):
    """Standalone sessions and event check-ins in the visible units."""
    qs = Session.objects.filter(organization=organization)
    if visible_ids is None:
        return qs
    return qs.filter(Q(unit_id__in=visible_ids) | Q(event__units__in=visible_ids)).distinct()


def unit_sessions(organization, visible_ids, unit_id):
    qs = scoped_sessions(organization, visible_ids).filter(Q(unit_id=unit_id) | Q(event__units=unit_id))
    return Session.objects.filter(pk__in=qs.values("pk"))


class SessionViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """
    Unit meetings and their attendance. Seeing a unit's sessions needs `members.view` or
    `events.check_in` for that unit; taking attendance needs `events.check_in`.
    """

    serializer_class = SessionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]
    filterset_fields = {"unit": ["exact"], "date": ["gte", "lte"]}

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
        """Attendance rate of the last 8 sessions of a unit."""
        unit_id = request.query_params.get("unit")
        sessions = (
            list(
                unit_sessions(self.organization, self.visible_unit_ids(), unit_id)
                .filter(date__lte=date.today())
                .prefetch_related("records")
                .order_by("-date", "-id")[:8]
            )
            if unit_id
            else []
        )
        points = [
            {"id": s.pk, "title": s.title, "date": s.date, "rate": rate(s.records.all())} for s in reversed(sessions)
        ]
        rates = [p["rate"] for p in points if p["rate"] is not None]
        return Response({"points": points, "average": round(sum(rates) / len(rates)) if rates else None})


class AttendanceReportView(APIView):
    """
    Attendance per member across standalone sessions and event check-ins (one per event day).
    Filters: unit, date_from, date_to, mandatory (true/false), source (events/sessions).
    """

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        service = get_permission_service(request)
        if not (service.has_perm_anywhere("members.view") or service.has_perm_anywhere("events.check_in")):
            raise PermissionDenied()
        organization = get_active_membership(request).organization
        visible = visible_unit_ids(service)
        params = request.query_params
        unit_id = params.get("unit")
        if unit_id and visible is not None and int(unit_id) not in visible:
            raise PermissionDenied()
        base = unit_sessions(organization, visible, unit_id) if unit_id else scoped_sessions(organization, visible)
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

        per_member: dict[int, dict] = {}
        for s in sessions:
            for r in s.records.all():
                row = per_member.setdefault(r.member_id, {k: 0 for k in AttendanceStatus.values})
                row[r.status] += 1
        member_qs = Member.objects.filter(organization=organization)
        if unit_id:
            member_qs = member_qs.filter(
                Q(pk__in=per_member) | Q(unit_memberships__unit_id=unit_id, status=MemberStatus.ACTIVE)
            ).distinct()
        else:
            member_qs = member_qs.filter(pk__in=per_member)
        members = []
        for m in member_qs.order_by("first_name", "last_name"):
            c = per_member.get(m.pk, {k: 0 for k in AttendanceStatus.values})
            counted = c["present"] + c["absent"]
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
        absent = sum(m["absent"] for m in members)
        occasions = [
            {
                "id": s.pk,
                "event": s.event_id,
                "title": s.title,
                "date": s.date,
                "source": "event" if s.event_id else "session",
                "is_mandatory": s.is_mandatory,
                "rate": rate(s.records.all()),
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
