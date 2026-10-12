from django.db import transaction
from django.db.models import Prefetch
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin
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
        ids = set()
        for codename in ("members.view", "events.check_in"):
            scope = self.service.unit_ids(codename)
            if scope is None and self.service.has_perm(codename):
                return None
            ids |= scope or set()
        return ids

    def require_check_in(self, unit):
        if not self.service.has_perm("events.check_in", unit):
            raise PermissionDenied(_("You can't take attendance for this unit."))

    def get_queryset(self):
        qs = Session.objects.filter(organization=self.organization).select_related("unit").prefetch_related("records")
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
                    AttendanceRecord.objects.update_or_create(
                        session=session,
                        member_id=row["member"],
                        defaults={"status": row["status"], "note": row["note"]},
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
        sessions = list(self.get_queryset().filter(unit_id=unit_id).order_by("-date", "-id")[:8]) if unit_id else []
        points = [
            {"id": s.pk, "title": s.title, "date": s.date, "rate": rate(s.records.all())} for s in reversed(sessions)
        ]
        rates = [p["rate"] for p in points if p["rate"] is not None]
        return Response({"points": points, "average": round(sum(rates) / len(rates)) if rates else None})
