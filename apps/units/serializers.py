from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import Patrol, Unit, UnitMembership, UnitPosition


class UnitBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = ["id", "name_en", "name_ar", "color"]


class UnitLeaderSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(source="member.id")
    full_name = serializers.CharField(source="member.full_name")
    position = serializers.CharField()


class PatrolSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Patrol
        fields = ["id", "unit", "name", "name_ar", "member_count"]

    def validate_unit(self, unit):
        if unit.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown unit."))
        return unit


class UnitSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)
    leaders = serializers.SerializerMethodField()

    class Meta:
        model = Unit
        fields = [
            "id",
            "name_en",
            "name_ar",
            "section",
            "age_min",
            "age_max",
            "color",
            "description",
            "is_active",
            "member_count",
            "leaders",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_leaders(self, obj):
        leaders = [m for m in obj.memberships.all() if m.position in (UnitPosition.LEADER, UnitPosition.ASSISTANT)]
        return UnitLeaderSerializer(leaders, many=True).data

    def validate_name_en(self, value):
        org = self.context["organization"]
        qs = Unit.objects.filter(organization=org, name_en__iexact=value.strip())
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_("A unit with this name already exists."))
        return value.strip()

    def validate(self, attrs):
        age_min = attrs.get("age_min", getattr(self.instance, "age_min", None))
        age_max = attrs.get("age_max", getattr(self.instance, "age_max", None))
        if age_min is not None and age_max is not None and age_min > age_max:
            raise serializers.ValidationError({"age_max": _("Must be greater than or equal to the minimum age.")})
        return attrs


class PatrolBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patrol
        fields = ["id", "name", "name_ar"]


class UnitMembershipSerializer(serializers.ModelSerializer):
    unit = UnitBriefSerializer(read_only=True)
    patrol = PatrolBriefSerializer(read_only=True)

    class Meta:
        model = UnitMembership
        fields = ["id", "unit", "position", "patrol", "start_date", "end_date"]


class UnitMembershipWriteSerializer(serializers.Serializer):
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all())
    position = serializers.ChoiceField(choices=UnitPosition.choices, default=UnitPosition.MEMBER)
    patrol = serializers.PrimaryKeyRelatedField(queryset=Patrol.objects.all(), allow_null=True, required=False)

    def validate_unit(self, unit):
        if unit.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown unit."))
        return unit

    def validate(self, attrs):
        patrol = attrs.get("patrol")
        if patrol is not None and patrol.unit_id != attrs["unit"].pk:
            raise serializers.ValidationError({"patrol": _("This patrol belongs to another unit.")})
        return attrs
