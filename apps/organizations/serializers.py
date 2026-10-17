from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.accounts.models import Language
from apps.accounts.serializers import (
    UserSerializer,
    validate_email_value,
    validate_username_value,
)
from apps.permissions.serializers import (
    RoleAssignmentSerializer,
    RoleAssignmentWriteSerializer,
)
from apps.units.serializers import (
    UnitMembershipSerializer,
    UnitMembershipWriteSerializer,
)

from .models import (
    Invitation,
    MembershipStatus,
    Organization,
    OrganizationSettings,
    OrgMembership,
)

User = get_user_model()


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "slug",
            "logo",
            "country",
            "currency",
            "timezone",
            "default_language",
            "fiscal_year_start_month",
            "created_at",
        ]
        read_only_fields = ["id", "slug", "created_at"]

    def validate_fiscal_year_start_month(self, value):
        if not 1 <= value <= 12:
            raise serializers.ValidationError(_("Choose a month between 1 and 12."))
        return value


class OrganizationSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationSettings
        fields = list(OrganizationSettings.THEME_COLOR_FIELDS) + ["default_theme_mode", "updated_at"]
        read_only_fields = ["updated_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        for field in OrganizationSettings.THEME_COLOR_FIELDS:
            if field in data:
                data[field] = (data[field] or "").strip().upper()
        return super().to_internal_value(data)


class MyOrganizationSerializer(serializers.ModelSerializer):
    """An organization the current user belongs to, as listed in the org switcher."""

    organization = OrganizationSerializer(read_only=True)
    theme = OrganizationSettingsSerializer(source="organization.settings", read_only=True)
    roles = serializers.SerializerMethodField()

    class Meta:
        model = OrgMembership
        fields = ["id", "organization", "is_owner", "status", "roles", "theme"]

    def get_roles(self, obj):
        """Distinct roles held in this organization, as {key, name} (key is set for default roles)."""
        roles = {a.role.pk: a.role for a in obj.role_assignments.all()}
        return [{"key": r.key, "name": r.name} for r in sorted(roles.values(), key=lambda r: (not r.is_system, r.name))]


# ---------------------------------------------------------------- sign-up


class SignupAdminSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    preferred_language = serializers.ChoiceField(choices=Language.choices, required=False)

    def validate_username(self, value):
        return validate_username_value(value)

    def validate_email(self, value):
        return validate_email_value(value)

    def validate(self, attrs):
        password_validation.validate_password(
            attrs["password"],
            User(username=attrs["username"], email=attrs.get("email"), first_name=attrs["first_name"]),
        )
        return attrs


class SignupOrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["name", "country", "currency", "timezone", "default_language"]


class SignupUnitSerializer(serializers.Serializer):
    name_en = serializers.CharField(max_length=120)
    name_ar = serializers.CharField(max_length=120, required=False, allow_blank=True)
    section = serializers.CharField(max_length=60, required=False, allow_blank=True)


class SignupBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationSettings
        fields = ["primary_color", "secondary_color"]


class SignupSerializer(serializers.Serializer):
    organization = SignupOrganizationSerializer()
    admin = SignupAdminSerializer()
    units = SignupUnitSerializer(many=True, required=False)
    branding = SignupBrandingSerializer(required=False)

    def validate_units(self, units):
        names = [u["name_en"].strip().lower() for u in units]
        if len(names) != len(set(names)):
            raise serializers.ValidationError(_("Unit names must be unique."))
        return units


# ---------------------------------------------------------------- members ("users")


class InvitationSerializer(serializers.ModelSerializer):
    url = serializers.CharField(read_only=True)

    class Meta:
        model = Invitation
        fields = ["code", "url", "expires_at", "created_at"]


class MemberSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    roles = RoleAssignmentSerializer(source="role_assignments", many=True, read_only=True)
    units = serializers.SerializerMethodField()
    member_id = serializers.SerializerMethodField()
    member = serializers.SerializerMethodField()
    guardian = serializers.SerializerMethodField()
    invitation = serializers.SerializerMethodField()

    class Meta:
        model = OrgMembership
        fields = [
            "id",
            "user",
            "status",
            "is_owner",
            "member_id",
            "member",
            "guardian",
            "roles",
            "units",
            "invitation",
            "created_at",
        ]

    def _member(self, obj):
        try:
            return obj.member
        except ObjectDoesNotExist:
            return None

    def get_member_id(self, obj):
        member = self._member(obj)
        return member.pk if member else None

    def get_member(self, obj):
        """The person on the Members page this login belongs to, if any."""
        member = self._member(obj)
        return {"id": member.pk, "full_name": member.full_name, "kind": member.kind} if member else None

    def get_guardian(self, obj):
        try:
            guardian = obj.guardian
        except ObjectDoesNotExist:
            return None
        return {"id": guardian.pk, "full_name": guardian.full_name}

    def get_units(self, obj):
        member = self._member(obj)
        return UnitMembershipSerializer(member.unit_memberships.all(), many=True).data if member else []

    def get_invitation(self, obj):
        if obj.status != MembershipStatus.INVITED or not self.context.get("can_manage_users"):
            return None
        invitation = next((i for i in obj.invitations.all() if i.is_usable), None)
        return InvitationSerializer(invitation).data if invitation else None


class MemberCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    preferred_language = serializers.ChoiceField(choices=Language.choices, required=False)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    invite = serializers.BooleanField(
        default=False, help_text=_("Generate an invitation link/code instead of a password.")
    )
    roles = RoleAssignmentWriteSerializer(many=True, required=False)
    member_id = serializers.IntegerField(
        required=False, allow_null=True, help_text=_("The person on the Members page this login belongs to.")
    )
    guardian_id = serializers.IntegerField(
        required=False, allow_null=True, help_text=_("An existing guardian (contact) this login belongs to.")
    )

    def validate_username(self, value):
        return validate_username_value(value) if value else ""

    def validate_email(self, value):
        return validate_email_value(value)

    def validate(self, attrs):
        if not attrs.get("invite"):
            if not attrs.get("username"):
                raise serializers.ValidationError({"username": _("Required when setting a password.")})
            if not attrs.get("password"):
                raise serializers.ValidationError({"password": _("Set a password or choose to send an invitation.")})
        if attrs.get("password"):
            password_validation.validate_password(
                attrs["password"], User(username=attrs.get("username", ""), first_name=attrs["first_name"])
            )
        return attrs


class LinkPeopleSerializer(serializers.Serializer):
    """Which member and which guardian a login belongs to; null unlinks, a missing key keeps it."""

    member_id = serializers.IntegerField(required=False, allow_null=True)
    guardian_id = serializers.IntegerField(required=False, allow_null=True)


class MemberUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, required=False)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    username = serializers.CharField(max_length=150, required=False)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    preferred_language = serializers.ChoiceField(choices=Language.choices, required=False)
    status = serializers.ChoiceField(
        choices=[
            (MembershipStatus.ACTIVE, MembershipStatus.ACTIVE.label),
            (MembershipStatus.DISABLED, MembershipStatus.DISABLED.label),
        ],
        required=False,
    )

    def validate_username(self, value):
        return validate_username_value(value, self.instance.user)

    def validate_email(self, value):
        return validate_email_value(value, self.instance.user)

    def validate_status(self, value):
        membership = self.instance
        if membership.status == MembershipStatus.INVITED and value == MembershipStatus.ACTIVE:
            raise serializers.ValidationError(_("Invited users become active when they accept their invitation."))
        if membership.is_owner and value == MembershipStatus.DISABLED:
            raise serializers.ValidationError(_("The organization owner can't be disabled."))
        return value

    def update(self, membership, validated_data):
        user = membership.user
        for field in ("first_name", "last_name", "username", "email", "preferred_language"):
            if field in validated_data:
                setattr(user, field, validated_data[field])
        user.save()
        if "status" in validated_data:
            membership.status = validated_data["status"]
            membership.save(update_fields=["status", "updated_at"])
        return membership


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value):
        password_validation.validate_password(value, self.context.get("user"))
        return value


class UnitMembershipListSerializer(serializers.Serializer):
    units = UnitMembershipWriteSerializer(many=True)

    def validate_units(self, units):
        ids = [u["unit"].pk for u in units]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(_("Each unit can only be listed once."))
        return units


# ---------------------------------------------------------------- invitations (public)


class InvitationPublicSerializer(serializers.Serializer):
    code = serializers.CharField()
    organization = serializers.SerializerMethodField()
    invited_by = serializers.SerializerMethodField()
    first_name = serializers.CharField(source="membership.user.first_name")
    last_name = serializers.CharField(source="membership.user.last_name")
    email = serializers.EmailField(source="membership.user.email", allow_null=True)
    suggested_username = serializers.CharField(source="membership.user.username")
    is_usable = serializers.BooleanField()
    expires_at = serializers.DateTimeField()

    def get_organization(self, obj):
        org = obj.membership.organization
        request = self.context.get("request")
        logo = request.build_absolute_uri(org.logo.url) if org.logo and request else None
        return {"id": str(org.pk), "name": org.name, "logo": logo}

    def get_invited_by(self, obj):
        return obj.created_by.full_name if obj.created_by else None


class AcceptInvitationSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, required=False)
    preferred_language = serializers.ChoiceField(choices=Language.choices, required=False)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate(self, attrs):
        request = self.context["request"]
        placeholder = self.context["invitation"].membership.user
        if request.user.is_authenticated and request.user.pk != placeholder.pk:
            return attrs  # joining with an existing account
        if not attrs.get("username"):
            raise serializers.ValidationError({"username": _("This field is required.")})
        if not attrs.get("password"):
            raise serializers.ValidationError({"password": _("This field is required.")})
        try:
            attrs["username"] = validate_username_value(attrs["username"], placeholder)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"username": exc.detail}) from exc
        try:
            attrs["email"] = validate_email_value(attrs.get("email"), placeholder)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"email": exc.detail}) from exc
        password_validation.validate_password(
            attrs["password"], User(username=attrs["username"], first_name=placeholder.first_name)
        )
        return attrs
