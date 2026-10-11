from django.db.models import Count, Prefetch, Q
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers import UserSerializer, tokens_for
from apps.accounts.views import AuthThrottle
from apps.audit.models import AuditLog
from apps.audit.serializers import AuditLogSerializer
from apps.audit.services import log_action
from apps.core.tenancy import TenantScopedMixin, get_active_membership
from apps.permissions.drf import HasOrgPermission, IsOrgMember, get_permission_service
from apps.permissions.models import RoleAssignment
from apps.permissions.serializers import (
    GrantListSerializer,
    PermissionGrantSerializer,
    RoleAssignmentListSerializer,
    RoleAssignmentSerializer,
)
from apps.permissions.service import PermissionService
from apps.units.models import Unit, UnitMembership, UnitPosition
from apps.units.serializers import UnitSerializer

from . import services
from .models import Invitation, MembershipStatus, OrgMembership
from .serializers import (
    AcceptInvitationSerializer,
    InvitationPublicSerializer,
    InvitationSerializer,
    MemberCreateSerializer,
    MemberSerializer,
    MemberUpdateSerializer,
    MyOrganizationSerializer,
    OrganizationSerializer,
    OrganizationSettingsSerializer,
    SetPasswordSerializer,
    SignupSerializer,
    UnitMembershipListSerializer,
)

LEADER_POSITIONS = (UnitPosition.LEADER, UnitPosition.ASSISTANT)
LEADER_ROLE_KEYS = ("org_admin", "unit_leader", "assistant_leader")


# ---------------------------------------------------------------- public: sign-up & invitations


class SignupView(APIView):
    """Create a new organization together with its first admin (the owner)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        membership = services.signup_organization(
            organization=data["organization"],
            admin=dict(data["admin"]),
            units=data.get("units", []),
            branding=data.get("branding", {}),
        )
        log_action(
            request,
            "organization.created",
            organization=membership.organization,
            target=membership.organization,
            actor=membership.user,
        )
        user = membership.user
        return Response(
            {
                **tokens_for(user),
                "user": UserSerializer(user).data,
                "membership": MyOrganizationSerializer(membership).data,
            },
            status=status.HTTP_201_CREATED,
        )


def _find_invitation(key: str) -> Invitation:
    key = key.strip()
    invitation = (
        Invitation.objects.select_related("membership__organization", "membership__user", "created_by")
        .filter(Q(token=key) | Q(code=key.upper()))
        .first()
    )
    if invitation is None:
        raise Http404
    return invitation


class InvitationPublicView(APIView):
    """Look up an invitation by its link token or its short code."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [AuthThrottle]

    def get(self, request, key):
        invitation = _find_invitation(key)
        return Response(InvitationPublicSerializer(invitation, context={"request": request}).data)


class AcceptInvitationView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request, key):
        invitation = _find_invitation(key)
        if not invitation.is_usable:
            raise ValidationError(
                {"detail": _("This invitation has expired or was already used.")}, code="invitation_unusable"
            )
        serializer = AcceptInvitationSerializer(
            data=request.data, context={"request": request, "invitation": invitation}
        )
        serializer.is_valid(raise_exception=True)
        membership = services.accept_invitation(invitation, data=serializer.validated_data, current_user=request.user)
        log_action(
            request,
            "invitation.accepted",
            organization=membership.organization,
            target=membership,
            actor=membership.user,
        )
        user = membership.user
        return Response(
            {
                **tokens_for(user),
                "user": UserSerializer(user).data,
                "membership": MyOrganizationSerializer(membership).data,
            }
        )


# ---------------------------------------------------------------- current user's organizations


class MyOrganizationsView(APIView):
    def get(self, request):
        memberships = (
            OrgMembership.objects.filter(
                user=request.user, status=MembershipStatus.ACTIVE, organization__is_active=True
            )
            .select_related("organization", "organization__settings")
            .prefetch_related("role_assignments__role")
            .order_by("organization__name")
        )
        return Response(MyOrganizationSerializer(memberships, many=True, context={"request": request}).data)


class MyPermissionsView(APIView):
    """Effective permissions in the active organization; drives what the UI shows."""

    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        membership = get_active_membership(request)
        service = get_permission_service(request)
        return Response(
            {
                "membership_id": membership.pk,
                "organization_id": str(membership.organization_id),
                "is_owner": membership.is_owner,
                "permissions": service.as_list(),
            }
        )


# ---------------------------------------------------------------- organization & settings


class CurrentOrganizationView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {"patch": "settings.manage"}
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        org = get_active_membership(request).organization
        return Response(OrganizationSerializer(org, context={"request": request}).data)

    def patch(self, request):
        org = get_active_membership(request).organization
        data = request.data.copy()
        if data.get("logo") == "":
            data["logo"] = None  # remove logo
        serializer = OrganizationSerializer(org, data=data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_action(
            request,
            "organization.updated",
            organization=org,
            target=org,
            changes={"fields": sorted(request.data.keys())},
        )
        return Response(serializer.data)


class OrganizationSettingsView(APIView):
    """Tenant-wide settings, incl. optional theme color overrides (readable by every member)."""

    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {"patch": "settings.manage"}

    def get(self, request):
        settings_obj = get_active_membership(request).organization.settings
        return Response(OrganizationSettingsSerializer(settings_obj).data)

    def patch(self, request):
        org = get_active_membership(request).organization
        before = OrganizationSettingsSerializer(org.settings).data
        serializer = OrganizationSettingsSerializer(org.settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        changes = {k: [before[k], v] for k, v in serializer.data.items() if k != "updated_at" and before.get(k) != v}
        log_action(request, "settings.updated", organization=org, target=org.settings, changes=changes)
        return Response(serializer.data)


# ---------------------------------------------------------------- members ("Users & permissions")


class MemberViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, HasOrgPermission]
    permission_map = {
        "list": "users.view",
        "retrieve": "users.view",
        "counts": "users.view",
        "permissions": "users.view",
        "create": "users.manage",
        "partial_update": "users.manage",
        "update": "users.manage",
        "destroy": "users.manage",
        "set_password": "users.manage",
        "units": "users.manage",
        "invitation": "users.manage",
        "roles": "permissions.manage",
        "set_permissions": "permissions.manage",
    }
    search_fields = ["user__username", "user__email", "user__first_name", "user__last_name"]
    ordering_fields = ["user__first_name", "user__last_name", "user__last_login", "created_at"]
    http_method_names = ["get", "post", "patch", "put", "delete"]

    def get_queryset(self):
        qs = (
            OrgMembership.objects.filter(organization=self.organization)
            .select_related("user")
            .prefetch_related(
                Prefetch("role_assignments", queryset=RoleAssignment.objects.select_related("role", "unit")),
                Prefetch("unit_memberships", queryset=UnitMembership.objects.select_related("unit")),
                "invitations",
            )
        )
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("role"):
            qs = qs.filter(role_assignments__role_id=params["role"])
        if params.get("unit"):
            qs = qs.filter(unit_memberships__unit_id=params["unit"])
        kind = params.get("kind")
        if kind == "leaders":
            qs = qs.filter(
                Q(unit_memberships__position__in=LEADER_POSITIONS) | Q(role_assignments__role__key__in=LEADER_ROLE_KEYS)
            )
        elif kind == "parents":
            qs = qs.filter(role_assignments__role__key="parent")
        elif kind == "scouts":
            qs = qs.filter(role_assignments__role__key="scout")
        return qs.distinct()

    def get_serializer_class(self):
        if self.action == "create":
            return MemberCreateSerializer
        if self.action in ("partial_update", "update"):
            return MemberUpdateSerializer
        return MemberSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["can_manage_users"] = get_permission_service(self.request).has_perm("users.manage")
        return context

    def _member_response(self, membership, status_code=status.HTTP_200_OK):
        membership = self.get_queryset().get(pk=membership.pk)
        return Response(MemberSerializer(membership, context=self.get_serializer_context()).data, status=status_code)

    def create(self, request, *args, **kwargs):
        serializer = MemberCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        membership, invitation = services.add_member(
            organization=self.organization, actor=request.user, data=serializer.validated_data
        )
        log_action(
            request,
            "member.created",
            organization=self.organization,
            target=membership,
            changes={"invited": invitation is not None},
        )
        return self._member_response(membership, status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        membership = self.get_object()
        partial = kwargs.pop("partial", False)
        if membership.pk == self.membership.pk and request.data.get("status") == MembershipStatus.DISABLED:
            raise ValidationError({"status": [_("You can't disable your own account.")]})
        serializer = MemberUpdateSerializer(
            membership, data=request.data, partial=partial, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_action(
            request,
            "member.updated",
            organization=self.organization,
            target=membership,
            changes={"fields": sorted(serializer.validated_data.keys())},
        )
        return self._member_response(membership)

    def destroy(self, request, *args, **kwargs):
        membership = self.get_object()
        if membership.is_owner:
            raise ValidationError({"detail": _("The organization owner can't be removed.")})
        if membership.pk == self.membership.pk:
            raise ValidationError({"detail": _("You can't remove yourself.")})
        user = membership.user
        log_action(request, "member.removed", organization=self.organization, target=membership)
        membership.delete()
        if not user.memberships.exists() and not user.has_usable_password():
            user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def counts(self, request):
        base = OrgMembership.objects.filter(organization=self.organization)
        return Response(
            {
                "all": base.count(),
                "leaders": base.filter(
                    Q(unit_memberships__position__in=LEADER_POSITIONS)
                    | Q(role_assignments__role__key__in=LEADER_ROLE_KEYS)
                )
                .distinct()
                .count(),
                "parents": base.filter(role_assignments__role__key="parent").distinct().count(),
                "scouts": base.filter(role_assignments__role__key="scout").distinct().count(),
                "invited": base.filter(status=MembershipStatus.INVITED).count(),
            }
        )

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        """Admin sets a new password, e.g. for members who have no email address."""
        membership = self.get_object()
        serializer = SetPasswordSerializer(data=request.data, context={"user": membership.user})
        serializer.is_valid(raise_exception=True)
        membership.user.set_password(serializer.validated_data["password"])
        membership.user.save(update_fields=["password"])
        log_action(request, "member.password_set", organization=self.organization, target=membership)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def invitation(self, request, pk=None):
        """Create (or re-create) the invitation link/code for an invited member."""
        membership = self.get_object()
        if membership.status != MembershipStatus.INVITED:
            raise ValidationError({"detail": _("Only invited users have an invitation.")})
        invitation = services.create_invitation(membership, request.user)
        log_action(request, "invitation.created", organization=self.organization, target=membership)
        return Response(InvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["put"])
    def units(self, request, pk=None):
        membership = self.get_object()
        serializer = UnitMembershipListSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        services.replace_unit_memberships(membership, serializer.validated_data["units"])
        log_action(
            request,
            "member.units_changed",
            organization=self.organization,
            target=membership,
            changes={"units": [[u["unit"].pk, u["position"]] for u in serializer.validated_data["units"]]},
        )
        return self._member_response(membership)

    @action(detail=True, methods=["put"])
    def roles(self, request, pk=None):
        membership = self.get_object()
        serializer = RoleAssignmentListSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        services.replace_role_assignments(membership, serializer.validated_data["roles"], request.user)
        log_action(
            request,
            "member.roles_changed",
            organization=self.organization,
            target=membership,
            changes={
                "roles": [
                    [r["role"].name, getattr(r.get("unit"), "pk", None)] for r in serializer.validated_data["roles"]
                ]
            },
        )
        return self._member_response(membership)

    def _permissions_payload(self, membership):
        assignments = membership.role_assignments.select_related("role", "unit").prefetch_related("role__permissions")
        roles = []
        for assignment in assignments:
            data = RoleAssignmentSerializer(assignment).data
            perms = assignment.role.permissions.all()
            if assignment.unit_id is not None:
                perms = [p for p in perms if p.unit_scoped]
            data["permissions"] = sorted(p.codename for p in perms)
            roles.append(data)
        grants = membership.permission_grants.select_related("permission", "unit", "granted_by")
        return {
            "membership_id": membership.pk,
            "is_owner": membership.is_owner,
            "roles": roles,
            "grants": PermissionGrantSerializer(grants, many=True).data,
            "effective": PermissionService(membership).as_list() if membership.is_active else [],
        }

    @action(detail=True, methods=["get"])
    def permissions(self, request, pk=None):
        return Response(self._permissions_payload(self.get_object()))

    @permissions.mapping.put
    def set_permissions(self, request, pk=None):
        """Replace the member's extra permission grants (on top of their roles)."""
        membership = self.get_object()
        serializer = GrantListSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        diff = services.replace_permission_grants(membership, serializer.validated_data["grants"], request.user)
        if diff["added"] or diff["removed"]:
            log_action(
                request, "member.permissions_changed", organization=self.organization, target=membership, changes=diff
            )
        return Response(self._permissions_payload(membership))


# ---------------------------------------------------------------- dashboard


class DashboardSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsOrgMember]

    def get(self, request):
        membership = get_active_membership(request)
        org = membership.organization
        service = get_permission_service(request)
        members = OrgMembership.objects.filter(organization=org)

        units = (
            Unit.objects.filter(organization=org, is_active=True)
            .annotate(member_count=Count("memberships", distinct=True))
            .prefetch_related(
                Prefetch("memberships", queryset=UnitMembership.objects.select_related("membership__user"))
            )
            .order_by("age_min", "name_en")
        )
        recent = []
        if service.has_perm("audit.view"):
            recent = AuditLogSerializer(
                AuditLog.objects.filter(organization=org).select_related("actor")[:6], many=True
            ).data

        return Response(
            {
                "counts": {
                    "members": members.exclude(status=MembershipStatus.DISABLED).count(),
                    "units": units.count(),
                    "leaders": members.filter(unit_memberships__position__in=LEADER_POSITIONS).distinct().count(),
                    "pending_invitations": members.filter(status=MembershipStatus.INVITED).count(),
                },
                "setup": {
                    "organization_created": True,
                    "units_added": units.exists(),
                    "leaders_invited": members.count() > 1,
                    "branding_set": org.settings.has_custom_colors() or bool(org.logo),
                    "members_imported": False,  # Members module arrives in phase 2
                },
                "units": UnitSerializer(units, many=True, context={"organization": org}).data,
                "recent_activity": recent,
            }
        )
