from datetime import date

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Case, F, IntegerField, OuterRef, Prefetch, Q, Subquery, TextField, Value, When
from django.db.models.functions import Lower, NullIf
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers import validate_email_value, validate_username_value
from apps.attendance.models import AttendanceRecord
from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.customfields.serializers import CustomFieldSerializer
from apps.organizations.models import MembershipStatus, OrgMembership
from apps.organizations.serializers import InvitationSerializer
from apps.organizations.services import create_invitation, generate_username
from apps.permissions.drf import IsOrgMember, get_permission_service
from apps.units.models import Unit, UnitMembership
from apps.units.serializers import UnitMembershipSerializer, UnitMembershipWriteSerializer

from . import exporter, services
from .importer import Importer, parse_json_field
from .models import (
    ChangeRequest,
    ChangeRequestItem,
    CustomField,
    Guardian,
    GuardianLink,
    MedicalInfo,
    Member,
    MemberKind,
    MemberStatus,
    RequestStatus,
)
from .sample import build_sample
from .serializers import (
    BulkActionSerializer,
    CertificationSerializer,
    ChangeRequestCreateSerializer,
    ChangeRequestSerializer,
    GiveAccessSerializer,
    GuardianLinkSerializer,
    GuardianLinkWriteSerializer,
    MedicalSerializer,
    MemberDetailSerializer,
    MemberListSerializer,
    MemberWriteSerializer,
    ReviewSerializer,
)

User = get_user_model()


def member_queryset(organization):
    return (
        Member.objects.filter(organization=organization)
        .select_related("membership__user", "medical")
        .prefetch_related(
            Prefetch("unit_memberships", queryset=UnitMembership.objects.select_related("unit", "patrol")),
            Prefetch("guardian_links", queryset=GuardianLink.objects.select_related("guardian")),
            "change_requests__guardian",
        )
    )


def _text(field: str):
    """Case-insensitive text sort key; empty strings count as missing so they sort last."""
    return NullIf(Lower(field), Value(""), output_field=TextField())


# Sort keys for the members list (`?sort=<column>` or `?sort=-<column>`), one or more expressions each.
MEMBER_SORTS = {
    "name": lambda: [Lower("first_name"), Lower("last_name")],
    "name_ar": lambda: [_text("first_name_ar"), _text("last_name_ar")],
    "kind": lambda: [F("kind")],
    "gender": lambda: [_text("gender")],
    "date_of_birth": lambda: [F("date_of_birth")],
    "join_date": lambda: [F("join_date")],
    "phone": lambda: [_text("phone")],
    "email": lambda: [_text("email")],
    "address": lambda: [_text("address")],
    "notes": lambda: [_text("notes")],
    "status": lambda: [F("status")],
    "has_login": lambda: [Case(When(membership__isnull=False, then=1), default=0, output_field=IntegerField())],
    "units": lambda: [
        Subquery(
            UnitMembership.objects.filter(member=OuterRef("pk"))
            .annotate(n=Lower("unit__name_en"))
            .order_by("n")
            .values("n")[:1]
        )
    ],
    "guardians": lambda: [
        Subquery(
            GuardianLink.objects.filter(member=OuterRef("pk"))
            .annotate(n=Lower("guardian__first_name"))
            .order_by("-is_primary", "n")
            .values("n")[:1]
        )
    ],
}


def sort_members(qs, param: str, organization):
    """Order the list by one column. Missing values go last in both directions; id breaks ties."""
    descending = param.startswith("-")
    column = param.lstrip("-")
    if column == "age":  # older = earlier birth date
        column, descending = "date_of_birth", not descending
    if column in MEMBER_SORTS:
        keys = MEMBER_SORTS[column]()
    elif (
        column.startswith("custom__")
        and CustomField.objects.for_entity(organization, "member").filter(key=column.removeprefix("custom__")).exists()
    ):
        keys = [F(f"custom_data__{column.removeprefix('custom__')}")]
    else:
        return qs
    # Annotate so the keys are part of the SELECT (required with DISTINCT on Postgres).
    qs = qs.annotate(**{f"_sort{i}": key for i, key in enumerate(keys)})
    order = [
        F(f"_sort{i}").desc(nulls_last=True) if descending else F(f"_sort{i}").asc(nulls_last=True)
        for i in range(len(keys))
    ]
    return qs.order_by(*order, "-id" if descending else "id")


def attendance_summary(member: Member, limit: int = 10) -> dict:
    records = list(
        AttendanceRecord.objects.filter(member=member)
        .select_related("session", "session__unit")
        .order_by("-session__date", "-session__id")[:limit]
    )
    counted = [r for r in records if r.status != "excused"]
    present = sum(r.status == "present" for r in records)
    return {
        "rate": round(100 * present / len(counted)) if counted else None,
        # Excused absences counted as missed: how much was missed, whatever the reason.
        "rate_strict": round(100 * present / len(records)) if records else None,
        "recent": [
            {
                "session_id": r.session_id,
                "event_id": r.session.event_id,
                "title": r.session.title,
                "date": r.session.date,
                "unit": r.session.unit.name_en if r.session.unit else "",
                "is_mandatory": r.session.is_mandatory,
                "status": r.status,
                "note": r.note,
            }
            for r in records
        ],
    }


class MemberViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """
    People in the organization. Visibility and edits are unit-scoped:
    `members.view` / `members.edit` granted for a unit only cover that unit's members.
    """

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    search_fields = ["first_name", "last_name", "first_name_ar", "last_name_ar", "guardian_links__guardian__first_name"]
    ordering_fields = ["first_name", "last_name", "date_of_birth", "created_at"]
    http_method_names = ["get", "post", "patch", "put", "delete"]

    # -------------------------------------------------- helpers

    @property
    def service(self):
        return get_permission_service(self.request)

    def require(self, codename: str, member: Member | None = None):
        if member is None:
            ok = self.service.has_perm_anywhere(codename)
        else:
            ok = services.can_on_member(self.service, codename, member)
        if not ok:
            raise PermissionDenied()

    def require_units_in_scope(self, units, codename="members.edit"):
        scope = self.service.unit_ids(codename)
        if scope is None:
            return
        bad = [u for u in units if u.pk not in scope]
        if bad:
            raise PermissionDenied(_("You can only add members to your own units."))

    def get_queryset(self):
        qs = member_queryset(self.organization)
        if not self.service.has_perm_anywhere("members.view"):
            return qs.none()
        qs = services.scope_members(qs, self.service.unit_ids("members.view"))
        p = self.request.query_params
        tab = p.get("tab")
        if tab == "youth":
            qs = qs.filter(kind=MemberKind.YOUTH).exclude(status=MemberStatus.ALUMNI)
        elif tab == "leaders":
            qs = qs.filter(unit_memberships__position__in=services.LEADER_POSITIONS).exclude(status=MemberStatus.ALUMNI)
        elif tab == "alumni":
            qs = qs.filter(status=MemberStatus.ALUMNI)
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("kind"):
            qs = qs.filter(kind=p["kind"])
        # One unit or several (comma separated): members in any of them.
        unit_ids = [int(u) for u in p.get("unit", "").split(",") if u.strip().isdigit()]
        if unit_ids:
            qs = qs.filter(unit_memberships__unit_id__in=unit_ids)
        if p.get("position"):
            qs = qs.filter(unit_memberships__position=p["position"])
        if p.get("patrol"):
            qs = qs.filter(unit_memberships__patrol_id=p["patrol"])
        today = date.today()
        if p.get("age_min", "").isdigit():
            qs = qs.filter(date_of_birth__lte=today.replace(year=today.year - int(p["age_min"])))
        if p.get("age_max", "").isdigit():
            qs = qs.filter(date_of_birth__gt=today.replace(year=today.year - int(p["age_max"]) - 1))
        if p.get("missing_info") in ("1", "true"):
            qs = qs.filter(services.missing_info_q())
        if p.get("has_login") in ("true", "false"):
            qs = qs.filter(membership__isnull=p["has_login"] == "false")
        if p.get("email", "").strip():
            qs = qs.filter(email__iexact=p["email"].strip())
        qs = qs.distinct()
        if p.get("sort"):
            qs = sort_members(qs, p["sort"], self.organization)
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return MemberListSerializer
        if self.action in ("create", "update", "partial_update"):
            return MemberWriteSerializer
        return MemberDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["permission_service"] = self.service
        return context

    def detail_response(self, member, status_code=status.HTTP_200_OK):
        member = member_queryset(self.organization).get(pk=member.pk)
        return Response(MemberDetailSerializer(member, context=self.get_serializer_context()).data, status=status_code)

    # -------------------------------------------------- CRUD

    def create(self, request, *args, **kwargs):
        self.require("members.edit")
        serializer = MemberWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        units = serializer.validated_data.pop("units", [])
        scope = self.service.unit_ids("members.edit")
        if scope is not None and not units:
            raise ValidationError({"units": [_("Add the member to at least one of your units.")]})
        self.require_units_in_scope([u["unit"] for u in units])
        with transaction.atomic():
            member = serializer.save(organization=self.organization)
            services.replace_unit_memberships(member, units)
        log_action(request, "person.created", organization=self.organization, target=member)
        return self.detail_response(member, status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        member = self.get_object()
        self.require("members.edit", member)
        partial = kwargs.pop("partial", False)
        serializer = MemberWriteSerializer(
            member, data=request.data, partial=partial, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.validated_data.pop("units", None)
        serializer.save()
        log_action(
            request,
            "person.updated",
            organization=self.organization,
            target=member,
            changes={"fields": sorted(serializer.validated_data.keys())},
        )
        return self.detail_response(member)

    def destroy(self, request, *args, **kwargs):
        member = self.get_object()
        self.require("members.edit", member)
        log_action(request, "person.deleted", organization=self.organization, target=member)
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def counts(self, request):
        base = services.scope_members(
            Member.objects.filter(organization=self.organization),
            self.service.unit_ids("members.view") if self.service.has_perm_anywhere("members.view") else set(),
        )
        not_alumni = base.exclude(status=MemberStatus.ALUMNI)
        return Response(
            {
                "all": base.count(),
                "youth": not_alumni.filter(kind=MemberKind.YOUTH).count(),
                "leaders": not_alumni.filter(unit_memberships__position__in=services.LEADER_POSITIONS)
                .distinct()
                .count(),
                "alumni": base.filter(status=MemberStatus.ALUMNI).count(),
                "missing_info": base.filter(services.missing_info_q()).distinct().count(),
            }
        )

    # -------------------------------------------------- units, guardians

    @action(detail=True, methods=["put"])
    def units(self, request, pk=None):
        member = self.get_object()
        self.require("members.edit", member)
        serializer = UnitMembershipWriteSerializer(
            data=request.data.get("units", []), many=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        rows = serializer.validated_data
        if len({r["unit"].pk for r in rows}) != len(rows):
            raise ValidationError({"units": [_("Each unit can only be listed once.")]})
        scope = self.service.unit_ids("members.edit")
        if scope is not None:
            before_outside = {(u.unit_id, u.position) for u in member.unit_memberships.all() if u.unit_id not in scope}
            after_outside = {(r["unit"].pk, r["position"]) for r in rows if r["unit"].pk not in scope}
            if before_outside != after_outside:
                raise PermissionDenied(_("You can only change units you manage."))
        services.replace_unit_memberships(member, rows)
        log_action(
            request,
            "person.units_changed",
            organization=self.organization,
            target=member,
            changes={"units": [[r["unit"].pk, r["position"]] for r in rows]},
        )
        return self.detail_response(member)

    @action(detail=True, methods=["get", "put"])
    def guardians(self, request, pk=None):
        member = self.get_object()
        if request.method == "GET":
            return Response(GuardianLinkSerializer(member.guardian_links.select_related("guardian"), many=True).data)
        self.require("members.edit", member)
        serializer = GuardianLinkWriteSerializer(
            data=request.data.get("guardians", []),
            many=True,
            context={**self.get_serializer_context(), "member": member},
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            member.guardian_links.all().delete()
            seen = set()
            for row in serializer.validated_data:
                guardian = row.get("guardian")
                if guardian is None and row.get("membership"):
                    guardian = services.ensure_guardian(row["membership"])
                if guardian is None:
                    guardian = Guardian.objects.create(
                        organization=self.organization,
                        first_name=row["first_name"].strip(),
                        last_name=(row.get("last_name") or "").strip(),
                        phone=row.get("phone", ""),
                        email=row.get("email", ""),
                    )
                else:
                    for field in ("first_name", "last_name", "phone", "email"):
                        if row.get(field):
                            setattr(guardian, field, row[field])
                    guardian.save()
                if guardian.pk in seen:
                    continue
                seen.add(guardian.pk)
                GuardianLink.objects.create(
                    member=member, guardian=guardian, relationship=row["relationship"], is_primary=row["is_primary"]
                )
        log_action(request, "person.guardians_changed", organization=self.organization, target=member)
        return self.detail_response(member)

    # -------------------------------------------------- medical

    @action(detail=True, methods=["get", "put"])
    def medical(self, request, pk=None):
        member = self.get_object()
        self.require("members.view_medical", member)
        medical = MedicalInfo.objects.filter(member=member).select_related("updated_by").first()
        if request.method == "GET":
            log_action(request, "person.medical_viewed", organization=self.organization, target=member)
            if medical is None:
                return Response(
                    {**MedicalSerializer(MedicalInfo(member=member)).data, "updated_by": None, "updated_at": None}
                )
            return Response(MedicalSerializer(medical).data)
        self.require("members.edit", member)
        serializer = MedicalSerializer(medical, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = {k: ([dict(x) for x in v] if isinstance(v, list) else v) for k, v in serializer.validated_data.items()}
        medical = medical or MedicalInfo(member=member)
        for key, value in data.items():
            setattr(medical, key, value)
        medical.updated_by = request.user
        medical.save()
        log_action(
            request,
            "person.medical_changed",
            organization=self.organization,
            target=member,
            changes={"fields": sorted(data.keys())},
        )
        return Response(MedicalSerializer(medical).data)

    # -------------------------------------------------- certifications

    @action(detail=True, methods=["get", "post"])
    def certifications(self, request, pk=None):
        member = self.get_object()
        if request.method == "GET":
            return Response(CertificationSerializer(member.certifications.all(), many=True).data)
        self.require("members.edit", member)
        serializer = CertificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cert = serializer.save(member=member)
        log_action(
            request,
            "person.certification_added",
            organization=self.organization,
            target=member,
            changes={"name": cert.name},
        )
        return Response(CertificationSerializer(cert).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"certifications/(?P<cert_id>\d+)")
    def certification(self, request, pk=None, cert_id=None):
        member = self.get_object()
        self.require("members.edit", member)
        cert = get_object_or_404(member.certifications, pk=cert_id)
        if request.method == "DELETE":
            cert.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = CertificationSerializer(cert, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # -------------------------------------------------- attendance, access

    @action(detail=True, methods=["get"])
    def attendance(self, request, pk=None):
        return Response(attendance_summary(self.get_object()))

    @action(detail=True, methods=["post"], url_path="give-access")
    def give_access(self, request, pk=None):
        """Turn a member into a user (login), without creating a duplicate person."""
        member = self.get_object()
        if not self.service.has_perm("users.manage"):
            raise PermissionDenied()
        if member.membership_id:
            raise ValidationError({"detail": _("This member already has a login.")})
        serializer = GiveAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        username = data.get("username")
        username = (
            validate_username_value(username) if username else generate_username(member.first_name, member.last_name)
        )
        email = validate_email_value(data.get("email") or member.email or None)
        if not data["invite"]:
            if not data.get("password"):
                raise ValidationError({"password": [_("Set a password or send an invitation.")]})
            try:
                password_validation.validate_password(data["password"], User(username=username))
            except DjangoValidationError as exc:
                raise ValidationError({"password": exc.messages}) from exc
        with transaction.atomic():
            user = User(username=username, email=email, first_name=member.first_name, last_name=member.last_name)
            if data["invite"]:
                user.set_unusable_password()
            else:
                user.set_password(data["password"])
            user.save()
            login = OrgMembership.objects.create(
                organization=self.organization,
                user=user,
                status=MembershipStatus.INVITED if data["invite"] else MembershipStatus.ACTIVE,
            )
            member.membership = login
            member.save(update_fields=["membership", "updated_at"])
            invitation = create_invitation(login, request.user) if data["invite"] else None
        log_action(request, "person.access_given", organization=self.organization, target=member)
        payload = MemberDetailSerializer(
            member_queryset(self.organization).get(pk=member.pk), context=self.get_serializer_context()
        ).data
        payload["invitation"] = InvitationSerializer(invitation).data if invitation else None
        return Response(payload, status=status.HTTP_201_CREATED)

    # -------------------------------------------------- bulk, import, export

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        serializer = BulkActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        members = list(self.get_queryset().filter(pk__in=data["ids"]))
        for member in members:
            self.require("members.edit", member)
        if data["action"] == "add_to_unit":
            unit = get_object_or_404(Unit, organization=self.organization, pk=data.get("unit"))
            self.require_units_in_scope([unit])
            for member in members:
                UnitMembership.objects.get_or_create(member=member, unit=unit, defaults={"position": data["position"]})
        else:
            if not data.get("status"):
                raise ValidationError({"status": [_("This field is required.")]})
            for member in members:
                member.status = data["status"]
                member.save()  # one by one, so each keeps its left date in step
        log_action(
            request,
            "person.bulk_updated",
            organization=self.organization,
            changes={"action": data["action"], "count": len(members)},
        )
        return Response({"updated": len(members)})

    def _importer(self, request):
        if not self.service.has_perm("members.edit"):
            raise PermissionDenied(_("Importing needs “Edit members” for the whole organization."))
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [_("Choose a CSV or Excel file.")]})
        return Importer(self.organization, upload, parse_json_field(request.data.get("mapping"), "mapping"))

    @action(detail=False, methods=["post"], url_path="import/preview")
    def import_preview(self, request):
        return Response(self._importer(request).preview())

    @action(detail=False, methods=["get"], url_path="import/sample")
    def import_sample(self, request):
        if not self.service.has_perm("members.edit"):
            raise PermissionDenied(_("Importing needs “Edit members” for the whole organization."))
        response = HttpResponse(
            build_sample(self.organization, request.LANGUAGE_CODE),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="members-import-sample.xlsx"'
        return response

    @action(detail=False, methods=["post"], url_path="import/commit")
    def import_commit(self, request):
        importer = self._importer(request)
        decisions = parse_json_field(request.data.get("decisions"), "decisions") or {}
        result = importer.commit(decisions, request.user)
        log_action(request, "person.imported", organization=self.organization, changes=result)
        return Response(result)

    def _export_base(self):
        if not self.service.has_perm_anywhere("members.view"):
            raise PermissionDenied()
        # Everyone this login may see; the export picks its own units, statuses and groups.
        return services.scope_members(member_queryset(self.organization), self.service.unit_ids("members.view"))

    @action(detail=False, methods=["get"], url_path="export/fields")
    def export_fields(self, request):
        self._export_base()
        return Response(exporter.catalog_payload(self.organization, self.service))

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Excel file of the chosen people and columns (`?fields=a,b&units=1,2&groups=leaders,members`)."""
        base = self._export_base()
        lang = request.query_params.get("lang") or request.LANGUAGE_CODE
        req = exporter.parse_request(
            request.query_params, self.organization, self.service, "ar" if lang == "ar" else "en"
        )
        rows = exporter.build_rows(base, req, self.service)
        response = HttpResponse(
            exporter.write_workbook(rows, req, self.organization, self.service),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="members.xlsx"'
        response["Cache-Control"] = "no-store"
        medical = [f for f in req.fields if exporter.catalog(self.organization, self.service)[f][2] == "medical"]
        log_action(
            request,
            "person.exported",
            organization=self.organization,
            changes={
                "rows": len(rows),
                "fields": req.fields,
                "units": req.unit_ids,
                "groups": req.groups,
                "medical_fields": medical,
            },
        )
        return response


# ---------------------------------------------------------------- approvals (leaders)


class ChangeRequestViewSet(
    TenantScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ChangeRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get_queryset(self):
        service = get_permission_service(self.request)
        qs = (
            ChangeRequest.objects.filter(organization=self.organization)
            .select_related("member", "requested_by", "reviewed_by", "guardian")
            .prefetch_related(
                "items", "guardian__links", "member__unit_memberships__unit", "member__unit_memberships__patrol"
            )
        )
        if not service.has_perm_anywhere("members.edit"):
            return qs.none()
        scope = service.unit_ids("members.edit")
        if scope is not None:
            qs = qs.filter(member__unit_memberships__unit_id__in=scope).distinct()
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        service = get_permission_service(self.request)
        context["can_view_medical"] = lambda m: services.can_on_member(service, "members.view_medical", m)
        return context

    @action(detail=False, methods=["get"])
    def counts(self, request):
        qs = self.get_queryset()
        return Response({s: qs.filter(status=s).count() for s in RequestStatus.values})

    def _review(self, request, approve: bool):
        change = self.get_object()
        if change.status != RequestStatus.PENDING:
            raise ValidationError({"detail": _("This request was already reviewed.")})
        service = get_permission_service(request)
        if not services.can_on_member(service, "members.edit", change.member):
            raise PermissionDenied()
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if approve:
            ids = set(serializer.validated_data.get("approve_items") or [i.pk for i in change.items.all()])
            touches_medical = any(i.field.startswith("medical.") and i.pk in ids for i in change.items.all())
            if touches_medical and not services.can_on_member(service, "members.view_medical", change.member):
                raise PermissionDenied(_("Approving medical changes needs “View medical info”."))
        else:
            ids = set()
        services.review_change_request(
            change, approve_item_ids=ids, note=serializer.validated_data["note"], actor=request.user
        )
        log_action(request, f"change_request.{change.status}", organization=self.organization, target=change.member)
        change = self.get_queryset().get(pk=change.pk) if self.get_queryset().filter(pk=change.pk).exists() else change
        return Response(ChangeRequestSerializer(change, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._review(request, approve=True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._review(request, approve=False)


# ---------------------------------------------------------------- parents


def my_guardian(request) -> Guardian | None:
    membership = get_active_membership(request)
    return Guardian.objects.filter(membership=membership).first()


def child_payload(member: Member, organization, guardian: Guardian) -> dict:
    fields = [f for f in services.fields_for(member, organization) if f.visible_to_parents]
    medical = getattr(member, "medical", None)
    leaders = (
        UnitMembership.objects.filter(unit__in=[u.unit_id for u in member.unit_memberships.all()], position="leader")
        .select_related("member")
        .order_by("member__first_name")
    )
    link = next((ln for ln in member.guardian_links.all() if ln.guardian_id == guardian.pk), None)
    return {
        "id": member.pk,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "full_name": member.full_name,
        "first_name_ar": member.first_name_ar,
        "last_name_ar": member.last_name_ar,
        "name_ar": member.name_ar,
        "date_of_birth": member.date_of_birth,
        "age": member.age,
        "photo": member.photo.url if member.photo else None,
        "relationship": link.relationship if link else "",
        "phone": member.phone,
        "email": member.email,
        "address": member.address,
        "units": UnitMembershipSerializer(member.unit_memberships.all(), many=True).data,
        "leaders": sorted({lm.member.full_name for lm in leaders}),
        "attendance": attendance_summary(member, 10),
        "medical": MedicalSerializer(medical).data if medical else None,
        "custom_fields": CustomFieldSerializer(fields, many=True, context={"organization": organization}).data,
        "custom_data": {f.key: (member.custom_data or {}).get(f.key) for f in fields},
        "editable_fields": sorted(services.parent_editable_fields(member, organization)),
        "pending_requests": sum(1 for r in member.change_requests.all() if r.status == RequestStatus.PENDING),
    }


class MyChildrenView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        guardian = my_guardian(request)
        if guardian is None:
            return Response([])
        org = guardian.organization
        children = member_queryset(org).filter(guardian_links__guardian=guardian).distinct()
        return Response([child_payload(m, org, guardian) for m in children])


class MyChildView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get_child(self, request, pk):
        guardian = my_guardian(request)
        if guardian is None:
            raise PermissionDenied()
        child = member_queryset(guardian.organization).filter(guardian_links__guardian=guardian, pk=pk).first()
        if child is None:
            raise PermissionDenied()
        return guardian, child

    def get(self, request, pk):
        guardian, child = self.get_child(request, pk)
        return Response(child_payload(child, guardian.organization, guardian))

    def post(self, request, pk):
        """Submit a change request for this child."""
        guardian, child = self.get_child(request, pk)
        serializer = ChangeRequestCreateSerializer(
            data=request.data, context={"member": child, "organization": guardian.organization}
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            change = ChangeRequest.objects.create(
                organization=guardian.organization,
                member=child,
                requested_by=request.user,
                guardian=guardian,
                message=serializer.validated_data["message"],
            )
            ChangeRequestItem.objects.bulk_create(
                [
                    ChangeRequestItem(
                        request=change, field=field, old_value=services.current_value(child, field), new_value=value
                    )
                    for field, value in serializer.validated_data["changes"].items()
                ]
            )
        log_action(request, "change_request.created", organization=guardian.organization, target=child)
        return Response(ParentRequestSerializer(change).data, status=status.HTTP_201_CREATED)


class ParentRequestSerializer(ChangeRequestSerializer):
    pass


class MyRequestsView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        guardian = my_guardian(request)
        if guardian is None:
            return Response([])
        qs = (
            ChangeRequest.objects.filter(guardian=guardian)
            .select_related("member", "requested_by", "reviewed_by", "guardian")
            .prefetch_related("items", "guardian__links", "member__unit_memberships__unit")
        )
        return Response(ParentRequestSerializer(qs, many=True).data)


class MyFamilyView(APIView):
    """Is the current user a guardian here? Drives the parent menu."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        from apps.events.services import family_invitations, my_people, pending_actions
        from apps.inventory.models import BorrowerType, Loan

        def gear_out(membership) -> int:
            me, _guardian, children = my_people(membership)
            ids = list(children) + ([me.pk] if me else [])
            return Loan.objects.filter(
                organization=membership.organization,
                borrower_type=BorrowerType.MEMBER,
                member_id__in=ids,
                closed_at__isnull=True,
            ).count()

        membership = get_active_membership(request)
        guardian = my_guardian(request)
        events = {
            # Members with their own login and parents both get the "Events" page.
            "has_events": guardian is not None
            or Member.objects.filter(membership=membership).exists()
            and family_invitations(membership).exists(),
            "events_pending": pending_actions(membership),
            "gear_out": gear_out(membership),
        }
        if guardian is None:
            return Response({"is_guardian": False, "children": 0, "pending_requests": 0, **events})
        return Response(
            {
                "is_guardian": True,
                "children": guardian.links.count(),
                "pending_requests": guardian.requests.filter(status=RequestStatus.PENDING).count(),
                **events,
            }
        )


class GuardianOptionsView(APIView):
    """Logins holding the Parent role, which can be linked as a member's guardian."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        if not get_permission_service(request).has_perm_anywhere("members.edit"):
            raise PermissionDenied
        org = get_active_membership(request).organization
        q = request.query_params.get("search", "").strip()
        parents = (
            OrgMembership.objects.filter(organization=org, role_assignments__role__key="parent")
            .exclude(status=MembershipStatus.DISABLED)
            .select_related("user", "guardian")
            .distinct()
        )
        if q:
            parents = parents.filter(
                Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
                | Q(user__username__icontains=q)
                | Q(user__email__icontains=q)
            )
        options = []
        for m in parents.order_by("user__first_name", "user__last_name")[:30]:
            guardian = getattr(m, "guardian", None)
            options.append(
                {
                    "guardian_id": guardian.pk if guardian else None,
                    "membership_id": m.pk,
                    "full_name": guardian.full_name if guardian else m.user.full_name,
                    "first_name": guardian.first_name if guardian else m.user.first_name or m.user.username,
                    "last_name": guardian.last_name if guardian else m.user.last_name,
                    "email": (guardian.email if guardian else "") or m.user.email or "",
                    "phone": guardian.phone if guardian else "",
                    "has_login": True,
                }
            )
        return Response(options)


class GuardianContactsView(APIView):
    """Guardians without a login (contacts added on a member's Guardians tab), to give one a login."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        if not get_permission_service(request).has_perm("users.manage"):
            raise PermissionDenied
        org = get_active_membership(request).organization
        q = request.query_params.get("search", "").strip()
        email = request.query_params.get("email", "").strip()
        guardians = Guardian.objects.filter(organization=org, membership__isnull=True).prefetch_related(
            Prefetch("links", queryset=GuardianLink.objects.select_related("member"))
        )
        if q:
            guardians = guardians.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q)
            )
        if email:
            guardians = guardians.filter(email__iexact=email)
        return Response(
            [
                {
                    "id": g.pk,
                    "full_name": g.full_name,
                    "first_name": g.first_name,
                    "last_name": g.last_name,
                    "email": g.email,
                    "phone": g.phone,
                    "children": [link.member.full_name for link in g.links.all()],
                }
                for g in guardians.order_by("first_name", "last_name")[:30]
            ]
        )
