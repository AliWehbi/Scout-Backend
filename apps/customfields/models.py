from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel

from .registry import ENTITIES, ENTITY_CHOICES


class FieldType(models.TextChoices):
    TEXT = "text", _("Text")
    NUMBER = "number", _("Number")
    DATE = "date", _("Date")
    YES_NO = "yesno", _("Yes / No")
    CHOICE = "choice", _("Choice")
    MULTI = "multi", _("Multiple choice")


class AppliesTo(models.TextChoices):
    """Member fields only: which members the field is for."""

    YOUTH = "youth", _("Youth members")
    ADULT = "adult", _("Adults")
    ALL = "all", _("Everyone")


class FieldSection(models.TextChoices):
    """Member fields only: where the field shows on the profile."""

    ADDITIONAL = "additional", _("Additional information")
    MEDICAL = "medical", _("Medical & emergency")


class CustomFieldQuerySet(models.QuerySet):
    def for_entity(self, organization, entity: str):
        return self.filter(organization=organization, entity=entity)


class CustomField(TenantModel):
    """
    An extra field an organization adds to a kind of record (see registry.py). Values live in
    `<record>.custom_data[key]`; `key` is internal and never changes. `code` is optional and only
    used by documents.
    """

    entity = models.CharField(max_length=20, choices=ENTITY_CHOICES, default="member")
    key = models.SlugField(max_length=60)
    # The name part of the template code (event.<org code>_<code>); empty = not used in documents.
    code = models.CharField(max_length=40, blank=True)
    label_en = models.CharField(max_length=120)
    label_ar = models.CharField(max_length=120, blank=True)
    field_type = models.CharField(max_length=10, choices=FieldType.choices, default=FieldType.TEXT)
    # Choice fields: the stored values. `option_labels` gives each its English and Arabic label,
    # {"<value>": {"en": "...", "ar": "..."}}, used in the matching language.
    options = models.JSONField(default=list, blank=True)
    option_labels = models.JSONField(default=dict, blank=True)
    # Pre-filled on new records and used by documents when a record has no value.
    # Text values (here and in records) are {"en": "...", "ar": "..."}; either may be missing.
    default = models.JSONField(null=True, blank=True)
    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices, default=AppliesTo.ALL)
    # Event fields: the event types they apply to; none = every event.
    event_types = models.ManyToManyField("events.EventType", blank=True, related_name="custom_fields")
    section = models.CharField(max_length=12, choices=FieldSection.choices, default=FieldSection.ADDITIONAL)
    required = models.BooleanField(default=False)
    # Members: parents see it on their child's profile. Events: families see it on My events.
    visible_to_parents = models.BooleanField(default=True)
    editable_by_parents = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = CustomFieldQuerySet.as_manager()

    class Meta:
        # The table predates this app (fields used to be members-only); the data stayed in place.
        db_table = "members_customfield"
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "entity", "key"], name="custom_field_entity_key_unique"),
            models.UniqueConstraint(
                fields=["organization", "entity", "code"], condition=~Q(code=""), name="custom_field_entity_code_unique"
            ),
        ]

    @property
    def spec(self):
        return ENTITIES[self.entity]

    def placeholder(self, org_code: str) -> str:
        """The name inside the record's dict in documents, e.g. "cedar_meeting_point"."""
        return f"{org_code}_{self.code}" if self.code else ""

    def template_code(self, org_code: str) -> str:
        """The full template code, e.g. "event.cedar_meeting_point"."""
        if not self.code:
            return ""
        return f"{self.spec.code_root}.{self.placeholder(org_code)}"

    def applies(self, record) -> bool:
        scope = self.spec.scope
        if scope == "member_kind":
            return self.applies_to == AppliesTo.ALL or self.applies_to == record.kind
        if scope == "event_types":
            type_ids = [t.pk for t in self.event_types.all()]
            return not type_ids or record.event_type_id in type_ids
        return True

    def __str__(self):
        return self.label_en
