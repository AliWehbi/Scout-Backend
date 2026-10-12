from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.attendance.serializers import RecordWriteSerializer
from apps.members.models import Member
from apps.units.models import Unit

from .models import TYPE_ICONS, Event, EventInvitation, EventStatus, EventType, Response


class EventTypeSerializer(serializers.ModelSerializer):
    event_count = serializers.SerializerMethodField()

    class Meta:
        model = EventType
        fields = [
            "id",
            "name_en",
            "name_ar",
            "color",
            "icon",
            "default_mandatory",
            "default_requires_consent",
            "is_archived",
            "order",
            "event_count",
        ]
        read_only_fields = ["id", "order", "event_count"]

    def get_event_count(self, obj):
        return getattr(obj, "n_events", None)

    def validate_icon(self, value):
        if value not in TYPE_ICONS:
            raise serializers.ValidationError(_("Unknown icon."))
        return value

    def validate_name_en(self, value):
        value = value.strip()
        org = self.context["organization"]
        clash = EventType.objects.filter(organization=org, name_en__iexact=value)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(_("A type with this name already exists."))
        return value


class TypeBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventType
        fields = ["id", "name_en", "name_ar", "color", "icon"]


class EventSerializer(serializers.ModelSerializer):
    event_type = serializers.PrimaryKeyRelatedField(queryset=EventType.objects.all())
    type = TypeBriefSerializer(source="event_type", read_only=True)
    units = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), many=True, required=False)
    leaders = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all(), many=True, required=False)
    counts = serializers.SerializerMethodField()
    days = serializers.ListField(child=serializers.DateField(), read_only=True)
    # write-only helpers
    repeat_until = serializers.DateField(write_only=True, required=False, allow_null=True)
    resign_required = serializers.BooleanField(write_only=True, required=False, default=False)
    publish = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Event
        fields = [
            "id",
            "event_type",
            "type",
            "title",
            "description",
            "start_date",
            "start_time",
            "end_date",
            "end_time",
            "days",
            "location",
            "units",
            "leaders",
            "is_mandatory",
            "capacity",
            "reply_deadline",
            "cost",
            "requires_consent",
            "consent_text",
            "status",
            "series",
            "counts",
            "created_at",
            "repeat_until",
            "resign_required",
            "publish",
        ]
        read_only_fields = ["id", "status", "series", "created_at"]

    def get_counts(self, obj):
        if not hasattr(obj, "n_invited"):
            return None
        return {
            "invited": obj.n_invited,
            "going": obj.n_going,
            "maybe": obj.n_maybe,
            "not_going": obj.n_not_going,
            "no_reply": obj.n_no_reply,
            "waitlist": obj.n_waitlist,
            "signed": obj.n_signed,
        }

    def _org(self):
        return self.context["organization"]

    def validate_event_type(self, value):
        if value.organization_id != self._org().pk:
            raise serializers.ValidationError(_("Unknown type."))
        if value.is_archived and (self.instance is None or self.instance.event_type_id != value.pk):
            raise serializers.ValidationError(_("This type is archived."))
        return value

    def validate_units(self, units):
        if any(u.organization_id != self._org().pk for u in units):
            raise serializers.ValidationError(_("Unknown unit."))
        return units

    def validate_leaders(self, leaders):
        if any(m.organization_id != self._org().pk for m in leaders):
            raise serializers.ValidationError(_("Unknown member."))
        return leaders

    def validate(self, attrs):
        get = lambda k: attrs.get(k, getattr(self.instance, k, None))  # noqa: E731
        start_date, end_date = get("start_date"), get("end_date")
        if start_date and end_date:
            if end_date < start_date:
                raise serializers.ValidationError({"end_date": _("Must be on or after the start date.")})
            if (end_date - start_date).days > 30:
                raise serializers.ValidationError({"end_date": _("An event can last at most 31 days.")})
            start_time, end_time = get("start_time"), get("end_time")
            if end_date == start_date and start_time and end_time and end_time <= start_time:
                raise serializers.ValidationError({"end_time": _("Must be after the start time.")})
        if get("requires_consent") and not (get("consent_text") or "").strip():
            raise serializers.ValidationError({"consent_text": _("Write what parents are agreeing to.")})
        capacity = get("capacity")
        if capacity is not None and capacity < 1:
            raise serializers.ValidationError({"capacity": _("Must be at least 1.")})
        if self.instance is not None and attrs.get("repeat_until"):
            raise serializers.ValidationError({"repeat_until": _("Repeat can only be set when creating.")})
        return attrs


class InviteSerializer(serializers.Serializer):
    units = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    patrols = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    members = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    include_parents = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if not (attrs["units"] or attrs["patrols"] or attrs["members"]):
            raise serializers.ValidationError(_("Choose who to invite."))
        return attrs


class RespondSerializer(serializers.Serializer):
    response = serializers.ChoiceField(choices=Response.choices)


class SignSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    agree = serializers.BooleanField()

    def validate_agree(self, value):
        if not value:
            raise serializers.ValidationError(_("Tick the box to agree."))
        return value


class EventAttendanceSerializer(serializers.Serializer):
    date = serializers.DateField()
    records = RecordWriteSerializer(many=True)


def signature_payload(inv: EventInvitation) -> dict | None:
    sig = inv.valid_signature
    if sig is None:
        return None
    return {"name": sig.signer_name, "signed_at": sig.signed_at}


def invitation_row(inv: EventInvitation, position: int | None) -> dict:
    member = inv.member
    responder = inv.responded_by
    sig = inv.valid_signature
    return {
        "id": inv.pk,
        "member": member.pk,
        "full_name": member.full_name,
        "name_ar": member.name_ar,
        "units": [
            {"unit": um.unit.name_en, "unit_ar": um.unit.name_ar, "patrol": um.patrol.name if um.patrol else ""}
            for um in member.unit_memberships.all()
        ],
        "has_login": member.membership_id is not None,
        "include_parents": inv.include_parents,
        "response": inv.response,
        "waitlisted": inv.waitlisted,
        "waitlist_position": position,
        "responded_at": inv.responded_at,
        "responded_as": inv.responded_as,
        "responded_by": (responder.get_full_name() or responder.username) if responder else "",
        "signature": {"name": sig.signer_name, "signed_at": sig.signed_at} if sig else None,
        "needs_signature": inv.event.requires_consent and inv.response == Response.GOING and sig is None,
    }


def is_published(event: Event) -> bool:
    return event.status == EventStatus.PUBLISHED
