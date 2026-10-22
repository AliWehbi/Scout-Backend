"""
The kinds of record that can have custom fields. Adding one (units, equipment...) means:

1. a `custom_data = models.JSONField(default=dict, blank=True)` on its model,
2. an `Entity` here, saying how a field decides which records it applies to,
3. showing the fields on its form (the frontend reads `GET /custom-fields/?entity=...`).

Values always live in `<record>.custom_data[field.key]`. A field's optional template code is
`<code_root>.<organization code>_<code>`, e.g. `event.cedar_meeting_point`, so it can never clash
with the app's own placeholders.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    key: str
    label_en: str
    label_ar: str
    # Root of this record's placeholders: fields get codes like <code_root>.cedar_school.
    code_root: str
    # How a field picks its records: "member_kind" (youth / adults / everyone), "event_types" or "all".
    scope: str
    # Whether documents for this record exist yet. Codes can be set before they do, so the
    # templates written later can use them straight away.
    in_documents: bool = False


# Member documents are coming: member fields already take codes ({{ member.cedar_school }}).
MEMBER = Entity("member", "Members", "الأعضاء", code_root="member", scope="member_kind", in_documents=False)
EVENT = Entity("event", "Events", "الأنشطة", code_root="event", scope="event_types", in_documents=True)

ENTITIES: dict[str, Entity] = {e.key: e for e in (MEMBER, EVENT)}
ENTITY_CHOICES = [(e.key, e.label_en) for e in ENTITIES.values()]
