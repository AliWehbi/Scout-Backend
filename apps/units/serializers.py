from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import Unit, UnitMembership, UnitPosition


class UnitBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = ["id", "name_en", "name_ar", "color"]


class UnitLeaderSerializer(serializers.Serializer):
    membership_id = serializers.IntegerField(source="membership.id")
    full_name = serializers.CharField(source="membership.user.full_name")
    position = serializers.CharField()


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


class UnitMembershipSerializer(serializers.ModelSerializer):
    unit = UnitBriefSerializer(read_only=True)

    class Meta:
        model = UnitMembership
        fields = ["id", "unit", "position", "start_date", "end_date"]


class UnitMembershipWriteSerializer(serializers.Serializer):
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all())
    position = serializers.ChoiceField(choices=UnitPosition.choices, default=UnitPosition.MEMBER)

    def validate_unit(self, unit):
        if unit.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown unit."))
        return unit
