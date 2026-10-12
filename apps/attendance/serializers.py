from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.units.models import Unit

from .models import AttendanceRecord, AttendanceStatus, Session


class SessionSerializer(serializers.ModelSerializer):
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all())
    counts = serializers.SerializerMethodField()

    class Meta:
        model = Session
        fields = ["id", "unit", "title", "date", "start_time", "end_time", "location", "counts", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_unit(self, unit):
        if unit.organization_id != self.context["organization"].pk:
            raise serializers.ValidationError(_("Unknown unit."))
        return unit

    def validate(self, attrs):
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start and end and end <= start:
            raise serializers.ValidationError({"end_time": _("Must be after the start time.")})
        return attrs

    def get_counts(self, obj):
        counts = {s: 0 for s in AttendanceStatus.values}
        for record in obj.records.all():
            counts[record.status] += 1
        return counts


class RecordWriteSerializer(serializers.Serializer):
    member = serializers.IntegerField()
    status = serializers.ChoiceField(choices=AttendanceStatus.choices, allow_null=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class RecordListSerializer(serializers.Serializer):
    records = RecordWriteSerializer(many=True)


class AttendanceRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceRecord
        fields = ["member", "status", "note"]
