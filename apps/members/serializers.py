import re

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.units.serializers import UnitMembershipSerializer, UnitMembershipWriteSerializer

from . import services
from .models import (
    Certification,
    ChangeRequest,
    ChangeRequestItem,
    CustomField,
    FieldType,
    Guardian,
    GuardianLink,
    MedicalInfo,
    Member,
    Relationship,
)

PHONE_RE = re.compile(r"^[0-9+()\-.\s]{3,40}$")


def validate_phone(value: str) -> str:
    value = (value or "").strip()
    if value and not PHONE_RE.match(value):
        raise serializers.ValidationError(_("Enter a valid phone number."))
    return value


# ---------------------------------------------------------------- medical


class AllergySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    severity = serializers.ChoiceField(choices=["mild", "moderate", "severe"], default="moderate")
    notes = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class MedicationSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    dose = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    when = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class EmergencyContactSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    relationship = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=40)

    def validate_phone(self, value):
        return validate_phone(value)


class MedicalSerializer(serializers.ModelSerializer):
    allergies = AllergySerializer(many=True, required=False)
    medications = MedicationSerializer(many=True, required=False)
    emergency_contacts = EmergencyContactSerializer(many=True, required=False)
    updated_by = serializers.SerializerMethodField()

    class Meta:
        model = MedicalInfo
        fields = [
            "allergies",
            "medications",
            "emergency_contacts",
            "conditions",
            "blood_type",
            "doctor_name",
            "doctor_phone",
            "insurance",
            "notes",
            "updated_by",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def get_updated_by(self, obj):
        return obj.updated_by.full_name if obj.updated_by else None

    def validate_doctor_phone(self, value):
        return validate_phone(value)


# ---------------------------------------------------------------- guardians


class GuardianSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    has_login = serializers.SerializerMethodField()

    class Meta:
        model = Guardian
        fields = ["id", "first_name", "last_name", "full_name", "phone", "email", "has_login"]

    def get_has_login(self, obj):
        return obj.membership_id is not None

    def validate_phone(self, value):
        return validate_phone(value)


class GuardianLinkSerializer(serializers.ModelSerializer):
    guardian = GuardianSerializer(read_only=True)

    class Meta:
        model = GuardianLink
        fields = ["id", "guardian", "relationship", "is_primary"]


class GuardianLinkWriteSerializer(serializers.Serializer):
    """Link an existing guardian (`guardian_id`) or create one from the given details."""

    guardian_id = serializers.IntegerField(required=False, allow_null=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    relationship = serializers.ChoiceField(choices=Relationship.choices, default=Relationship.GUARDIAN)
    is_primary = serializers.BooleanField(default=False)

    def validate_phone(self, value):
        return validate_phone(value)

    def validate(self, attrs):
        org = self.context["organization"]
        if attrs.get("guardian_id"):
            guardian = Guardian.objects.filter(organization=org, pk=attrs["guardian_id"]).first()
            if guardian is None:
                raise serializers.ValidationError({"guardian_id": _("Unknown guardian.")})
            attrs["guardian"] = guardian
        elif not (attrs.get("first_name") or "").strip():
            raise serializers.ValidationError({"first_name": _("This field is required.")})
        return attrs


# ---------------------------------------------------------------- members


class MemberListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    name_ar = serializers.CharField(read_only=True)
    age = serializers.IntegerField(read_only=True)
    units = UnitMembershipSerializer(source="unit_memberships", many=True, read_only=True)
    guardians = serializers.SerializerMethodField()
    has_login = serializers.SerializerMethodField()
    flags = serializers.SerializerMethodField()

    class Meta:
        model = Member
        fields = [
            "id",
            "first_name",
            "last_name",
            "full_name",
            "first_name_ar",
            "last_name_ar",
            "name_ar",
            "kind",
            "status",
            "age",
            "date_of_birth",
            "gender",
            "join_date",
            "phone",
            "email",
            "address",
            "notes",
            "custom_data",
            "photo",
            "units",
            "guardians",
            "has_login",
            "flags",
        ]

    def get_guardians(self, obj):
        return [link.guardian.full_name for link in obj.guardian_links.all()]

    def get_has_login(self, obj):
        return obj.membership_id is not None

    def get_flags(self, obj):
        return services.missing_info_flags(obj)


class MemberDetailSerializer(MemberListSerializer):
    guardian_links = GuardianLinkSerializer(many=True, read_only=True)
    login = serializers.SerializerMethodField()
    access = serializers.SerializerMethodField()
    pending_request = serializers.SerializerMethodField()

    class Meta(MemberListSerializer.Meta):
        fields = MemberListSerializer.Meta.fields + [
            "guardian_links",
            "login",
            "access",
            "pending_request",
            "created_at",
            "updated_at",
        ]

    def get_login(self, obj):
        m = obj.membership
        if m is None:
            return None
        return {"membership_id": m.pk, "username": m.user.username, "status": m.status}

    def get_access(self, obj):
        """What the current user may do on this member (drives buttons in the UI)."""
        service = self.context.get("permission_service")
        if service is None:
            return {}
        return {
            "edit": services.can_on_member(service, "members.edit", obj),
            "view_medical": services.can_on_member(service, "members.view_medical", obj),
            "manage_users": service.has_perm("users.manage"),
        }

    def get_pending_request(self, obj):
        pending = [r for r in obj.change_requests.all() if r.status == "pending"]
        if not pending:
            return None
        latest = pending[0]
        guardian = latest.guardian.full_name if latest.guardian else None
        return {"id": latest.pk, "count": len(pending), "guardian": guardian, "created_at": latest.created_at}


class MemberWriteSerializer(serializers.ModelSerializer):
    units = UnitMembershipWriteSerializer(many=True, required=False, write_only=True)

    class Meta:
        model = Member
        fields = [
            "first_name",
            "last_name",
            "first_name_ar",
            "last_name_ar",
            "date_of_birth",
            "gender",
            "kind",
            "status",
            "join_date",
            "phone",
            "email",
            "address",
            "notes",
            "custom_data",
            "units",
        ]

    def validate_phone(self, value):
        return validate_phone(value)

    def validate_units(self, units):
        ids = [u["unit"].pk for u in units]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(_("Each unit can only be listed once."))
        return units

    def validate(self, attrs):
        org = self.context["organization"]
        probe = self.instance or Member(organization=org, kind=attrs.get("kind", "youth"))
        if "kind" in attrs:
            probe.kind = attrs["kind"]
        if "custom_data" in attrs or self.instance is None:
            attrs["custom_data"] = services.clean_custom_data(
                probe, org, attrs.get("custom_data") or {}, partial=self.instance is not None
            )
        return attrs


class BulkActionSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=500)
    action = serializers.ChoiceField(choices=["add_to_unit", "set_status"])
    unit = serializers.IntegerField(required=False)
    position = serializers.CharField(required=False, default="member")
    status = serializers.ChoiceField(choices=["active", "inactive", "alumni"], required=False)


class GiveAccessSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    invite = serializers.BooleanField(default=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)


# ---------------------------------------------------------------- certifications & custom fields


class CertificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Certification
        fields = ["id", "name", "issued_on", "expires_on", "reference", "notes", "created_at"]
        read_only_fields = ["id", "created_at"]


class CustomFieldSerializer(serializers.ModelSerializer):
    key = serializers.SlugField(max_length=60, required=False)

    class Meta:
        model = CustomField
        fields = [
            "id",
            "key",
            "label_en",
            "label_ar",
            "field_type",
            "options",
            "applies_to",
            "section",
            "required",
            "visible_to_parents",
            "editable_by_parents",
            "order",
            "is_active",
        ]
        read_only_fields = ["id"]

    def validate_options(self, options):
        if not isinstance(options, list) or any(not isinstance(o, str) or not o.strip() for o in options):
            raise serializers.ValidationError(_("Options must be a list of text values."))
        cleaned = [o.strip() for o in options]
        if len(cleaned) != len(set(cleaned)):
            raise serializers.ValidationError(_("Options must be unique."))
        return cleaned

    def validate(self, attrs):
        org = self.context["organization"]
        field_type = attrs.get("field_type", getattr(self.instance, "field_type", FieldType.TEXT))
        options = attrs.get("options", getattr(self.instance, "options", []))
        if field_type in (FieldType.CHOICE, FieldType.MULTI) and not options:
            raise serializers.ValidationError({"options": _("Add at least one option.")})
        if self.instance is None:
            key = attrs.get("key") or slugify(attrs.get("label_en", ""))[:50].replace("-", "_") or "field"
            base, n = key, 2
            while CustomField.objects.filter(organization=org, key=key).exists():
                key = f"{base}_{n}"
                n += 1
            attrs["key"] = key
        else:
            attrs.pop("key", None)  # keys are stable once created
        if attrs.get("editable_by_parents") and not attrs.get(
            "visible_to_parents", getattr(self.instance, "visible_to_parents", True)
        ):
            raise serializers.ValidationError({"editable_by_parents": _("Parents must be able to see the field.")})
        return attrs


# ---------------------------------------------------------------- change requests


class ChangeRequestItemSerializer(serializers.ModelSerializer):
    restricted = serializers.SerializerMethodField()

    class Meta:
        model = ChangeRequestItem
        fields = ["id", "field", "old_value", "new_value", "approved", "restricted"]

    def get_restricted(self, obj):
        return False

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if obj.field.startswith("medical.") and not self.context.get("can_view_medical", True):
            data["old_value"] = data["new_value"] = None
            data["restricted"] = True
        return data


class ChangeRequestSerializer(serializers.ModelSerializer):
    member = serializers.SerializerMethodField()
    requested_by = serializers.SerializerMethodField()
    guardian = serializers.SerializerMethodField()
    reviewed_by = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()

    class Meta:
        model = ChangeRequest
        fields = [
            "id",
            "member",
            "requested_by",
            "guardian",
            "message",
            "status",
            "items",
            "reviewed_by",
            "reviewed_at",
            "review_note",
            "created_at",
        ]

    def get_member(self, obj):
        m = obj.member
        return {
            "id": m.pk,
            "full_name": m.full_name,
            "name_ar": m.name_ar,
            "units": UnitMembershipSerializer(m.unit_memberships.all(), many=True).data,
        }

    def get_requested_by(self, obj):
        return obj.requested_by.full_name if obj.requested_by else None

    def get_guardian(self, obj):
        if obj.guardian is None:
            return None
        link = next((ln for ln in obj.guardian.links.all() if ln.member_id == obj.member_id), None)
        return {
            "id": obj.guardian_id,
            "full_name": obj.guardian.full_name,
            "relationship": link.relationship if link else "",
        }

    def get_reviewed_by(self, obj):
        return obj.reviewed_by.full_name if obj.reviewed_by else None

    def get_items(self, obj):
        can = self.context.get("can_view_medical")
        can_view = can(obj.member) if callable(can) else True
        return ChangeRequestItemSerializer(obj.items.all(), many=True, context={"can_view_medical": can_view}).data


class ReviewSerializer(serializers.Serializer):
    approve_items = serializers.ListField(child=serializers.IntegerField(), required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")


def clean_requested_change(member: Member, organization, field: str, value):
    """Validate one value a parent wants to set. Returns the cleaned value."""
    if field in ("phone",):
        return validate_phone(value)
    if field == "email":
        return serializers.EmailField(allow_blank=True).run_validation(value or "")
    if field == "address":
        return str(value or "").strip()[:255]
    if field.startswith("medical."):
        name = field.split(".", 1)[1]
        serializer = MedicalSerializer(data={name: value}, partial=True)
        serializer.is_valid(raise_exception=True)
        cleaned = serializer.validated_data.get(name)
        return [dict(x) for x in cleaned] if isinstance(cleaned, list) else cleaned
    if field.startswith("custom."):
        key = field.split(".", 1)[1]
        cf = CustomField.objects.get(organization=organization, key=key)
        return services.clean_custom_value(cf, value)
    raise serializers.ValidationError(_("This field can't be changed."))


class ChangeRequestCreateSerializer(serializers.Serializer):
    message = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")
    changes = serializers.DictField()

    def validate_changes(self, changes):
        member = self.context["member"]
        org = self.context["organization"]
        allowed = services.parent_editable_fields(member, org)
        cleaned, errors = {}, {}
        for field, value in changes.items():
            if field not in allowed:
                errors[field] = [_("This field can't be changed.")]
                continue
            try:
                new = clean_requested_change(member, org, field, value)
            except serializers.ValidationError as exc:
                errors[field] = exc.detail
                continue
            if new != services.current_value(member, field):
                cleaned[field] = new
        if errors:
            raise serializers.ValidationError(errors)
        if not cleaned:
            raise serializers.ValidationError(_("Nothing changed."))
        return cleaned
