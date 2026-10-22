import re

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.events.models import EventType

from . import services
from .models import AppliesTo, CustomField, FieldSection, FieldType
from .registry import ENTITIES

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class CustomFieldSerializer(serializers.ModelSerializer):
    key = serializers.SlugField(max_length=60, required=False)
    entity = serializers.ChoiceField(choices=list(ENTITIES), required=False)
    event_types = serializers.PrimaryKeyRelatedField(queryset=EventType.objects.all(), many=True, required=False)
    template_code = serializers.SerializerMethodField()

    class Meta:
        model = CustomField
        fields = [
            "id",
            "entity",
            "key",
            "code",
            "template_code",
            "label_en",
            "label_ar",
            "field_type",
            "options",
            "option_labels",
            "default",
            "applies_to",
            "event_types",
            "section",
            "required",
            "visible_to_parents",
            "editable_by_parents",
            "order",
            "is_active",
        ]
        read_only_fields = ["id", "order"]

    def get_template_code(self, obj):
        return obj.template_code(self.context["organization"].code)

    def validate_options(self, options):
        if not isinstance(options, list) or any(not isinstance(o, str) or not o.strip() for o in options):
            raise serializers.ValidationError(_("Options must be a list of text values."))
        cleaned = [o.strip() for o in options]
        if len(cleaned) != len(set(cleaned)):
            raise serializers.ValidationError(_("Options must be unique."))
        return cleaned

    def validate_option_labels(self, labels):
        if not isinstance(labels, dict):
            raise serializers.ValidationError(_("Send the option labels as an object."))
        cleaned = {}
        for value, pair in labels.items():
            if not isinstance(pair, dict):
                raise serializers.ValidationError(_("Each option needs an English and an Arabic label."))
            texts = services.localized_text({k: pair.get(k) for k in services.LANGS if isinstance(pair.get(k), str)})
            if texts:
                cleaned[str(value)] = texts
        return cleaned

    def validate_code(self, code):
        code = (code or "").strip().lower()
        if code and not CODE_RE.match(code):
            raise serializers.ValidationError(
                _("Start with a letter and use only lowercase letters, numbers and _ (40 at most).")
            )
        return code

    def validate(self, attrs):
        org = self.context["organization"]
        instance = self.instance
        if instance is not None:
            attrs.pop("key", None)  # keys are stable once created
            attrs.pop("entity", None)  # a field can't move to another kind of record
        entity = ENTITIES[attrs.get("entity") or (instance.entity if instance else "member")]
        if instance is None:
            attrs["entity"] = entity.key

        field_type = attrs.get("field_type", getattr(instance, "field_type", FieldType.TEXT))
        options = attrs.get("options", getattr(instance, "options", []))
        if field_type in (FieldType.CHOICE, FieldType.MULTI) and not options:
            raise serializers.ValidationError({"options": _("Add at least one option.")})
        if "option_labels" in attrs or "options" in attrs:
            # Labels only for options that exist; other types have none.
            labels = attrs.get("option_labels", getattr(instance, "option_labels", {}) or {})
            keep = set(options) if field_type in (FieldType.CHOICE, FieldType.MULTI) else set()
            attrs["option_labels"] = {k: v for k, v in labels.items() if k in keep}

        code = attrs.get("code", getattr(instance, "code", ""))
        if code:
            clash = CustomField.objects.for_entity(org, entity.key).filter(code=code)
            if instance is not None:
                clash = clash.exclude(pk=instance.pk)
            if clash.exists():
                raise serializers.ValidationError({"code": _("Another field already uses this code.")})

        if "default" in attrs or "field_type" in attrs or "options" in attrs:
            probe = CustomField(field_type=field_type, options=options)
            try:
                attrs["default"] = services.clean_value(probe, attrs.get("default", getattr(instance, "default", None)))
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({"default": exc.detail}) from None

        if entity.scope == "event_types":
            for event_type in attrs.get("event_types", []):
                if event_type.organization_id != org.pk:
                    raise serializers.ValidationError({"event_types": _("Unknown event type.")})
        else:
            attrs.pop("event_types", None)
        if entity.scope != "member_kind":
            # Member-only settings keep their defaults on other records.
            attrs["applies_to"], attrs["section"], attrs["editable_by_parents"] = (
                AppliesTo.ALL,
                FieldSection.ADDITIONAL,
                False,
            )
            if instance is None and "visible_to_parents" not in self.initial_data:
                attrs["visible_to_parents"] = False  # event fields are hidden from families unless switched on
        if entity.scope == "all":
            # Organization fields: one value (kept in `default`), nothing to require or show families.
            attrs["required"], attrs["visible_to_parents"] = False, False

        if instance is None:
            key = attrs.get("key") or slugify(attrs.get("label_en", ""))[:50].replace("-", "_") or "field"
            base, n = key, 2
            while CustomField.objects.for_entity(org, entity.key).filter(key=key).exists():
                key = f"{base}_{n}"
                n += 1
            attrs["key"] = key
        if attrs.get("editable_by_parents") and not attrs.get(
            "visible_to_parents", getattr(instance, "visible_to_parents", True)
        ):
            raise serializers.ValidationError({"editable_by_parents": _("Parents must be able to see the field.")})
        return attrs
