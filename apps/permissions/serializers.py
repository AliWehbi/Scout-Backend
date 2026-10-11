from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.units.models import Unit
from apps.units.serializers import UnitBriefSerializer

from .catalog import ALL_PERMISSIONS
from .models import Permission, PermissionGrant, Role, RoleAssignment


class CodenameListField(serializers.ListField):
    child = serializers.CharField()

    def to_representation(self, value):
        return sorted(p.codename for p in value.all())

    def to_internal_value(self, data):
        codenames = super().to_internal_value(data)
        unknown = sorted(set(codenames) - set(ALL_PERMISSIONS))
        if unknown:
            raise serializers.ValidationError(_("Unknown permissions: %(list)s") % {"list": ", ".join(unknown)})
        return list(Permission.objects.filter(codename__in=codenames))


class RoleSerializer(serializers.ModelSerializer):
    permissions = CodenameListField(required=False)
    assignment_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Role
        fields = ["id", "key", "name", "description", "is_system", "permissions", "assignment_count", "created_at"]
        read_only_fields = ["id", "key", "is_system", "created_at"]

    def validate_name(self, value):
        org = self.context["organization"]
        qs = Role.objects.filter(organization=org, name__iexact=value.strip())
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_("A role with this name already exists."))
        return value.strip()

    def create(self, validated_data):
        permissions = validated_data.pop("permissions", [])
        role = Role.objects.create(**validated_data)
        role.permissions.set(permissions)
        return role

    def update(self, instance, validated_data):
        permissions = validated_data.pop("permissions", None)
        instance = super().update(instance, validated_data)
        if permissions is not None:
            instance.permissions.set(permissions)
        return instance


class RoleBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["id", "key", "name"]


class RoleAssignmentSerializer(serializers.ModelSerializer):
    role = RoleBriefSerializer(read_only=True)
    unit = UnitBriefSerializer(read_only=True)

    class Meta:
        model = RoleAssignment
        fields = ["id", "role", "unit", "created_at"]


class _OrgScopedWriteMixin:
    def validate_unit(self, unit):
        if unit is not None and unit.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown unit."))
        return unit


class RoleAssignmentWriteSerializer(_OrgScopedWriteMixin, serializers.Serializer):
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all())
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), allow_null=True, required=False)

    def validate_role(self, role):
        if role.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown role."))
        return role


class PermissionGrantSerializer(serializers.ModelSerializer):
    permission = serializers.CharField(source="permission.codename")
    unit = UnitBriefSerializer(read_only=True)
    granted_by = serializers.SerializerMethodField()

    class Meta:
        model = PermissionGrant
        fields = ["id", "permission", "unit", "expires_at", "note", "granted_by", "created_at"]

    def get_granted_by(self, obj):
        if obj.granted_by is None:
            return None
        return {"id": obj.granted_by.pk, "full_name": obj.granted_by.full_name}


class PermissionGrantWriteSerializer(_OrgScopedWriteMixin, serializers.Serializer):
    permission = serializers.SlugRelatedField(slug_field="codename", queryset=Permission.objects.all())
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), allow_null=True, required=False)
    expires_at = serializers.DateTimeField(allow_null=True, required=False)
    note = serializers.CharField(allow_blank=True, required=False, max_length=255)

    def validate(self, attrs):
        if attrs.get("unit") is not None and not attrs["permission"].unit_scoped:
            raise serializers.ValidationError(
                {
                    "unit": _("“%(perm)s” can only be granted for the whole organization.")
                    % {"perm": attrs["permission"].codename}
                }
            )
        return attrs


def _unique_keys(items, key_fn, message):
    seen = set()
    for item in items:
        k = key_fn(item)
        if k in seen:
            raise serializers.ValidationError(message)
        seen.add(k)
    return items


class GrantListSerializer(serializers.Serializer):
    grants = PermissionGrantWriteSerializer(many=True)

    def validate_grants(self, grants):
        return _unique_keys(
            grants,
            lambda g: (g["permission"].pk, getattr(g.get("unit"), "pk", None)),
            _("The same permission is listed twice for the same scope."),
        )


class RoleAssignmentListSerializer(serializers.Serializer):
    roles = RoleAssignmentWriteSerializer(many=True)

    def validate_roles(self, roles):
        return _unique_keys(
            roles,
            lambda r: (r["role"].pk, getattr(r.get("unit"), "pk", None)),
            _("The same role is assigned twice for the same scope."),
        )
