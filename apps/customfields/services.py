import re
from datetime import date

from django.utils import formats, translation
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from .models import CustomField, FieldType

EMPTY = (None, "", [], {})
YES_NO = {True: ("Yes", "نعم"), False: ("No", "لا")}
LANGS = ("en", "ar")
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def guess_lang(text: str) -> str:
    """For a single text with no language given (imports, older values): Arabic letters mean Arabic."""
    return "ar" if ARABIC_RE.search(text) else "en"


def localized_text(value) -> dict:
    """A text value as {"en": ..., "ar": ...} (only the languages that have text)."""
    if isinstance(value, dict):
        pairs = ((lang, str(value.get(lang) or "").strip()[:500]) for lang in LANGS)
        return {lang: text for lang, text in pairs if text}
    text = str(value or "").strip()[:500]
    return {guess_lang(text): text} if text else {}


def pick(texts: dict, lang: str) -> str:
    """The text in `lang`, else in the other language."""
    other = "en" if lang == "ar" else "ar"
    return texts.get(lang) or texts.get(other) or ""


def option_label(field: CustomField, value, lang: str) -> str:
    labels = (field.option_labels or {}).get(str(value)) or {}
    return pick(labels, lang) or str(value)


def clean_value(field: CustomField, value):
    """Validate and normalize one value for `field`; raises ValidationError."""
    if value in EMPTY:
        return None
    t = field.field_type
    if t == FieldType.TEXT:
        return localized_text(value) or None
    if t == FieldType.NUMBER:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(_("Enter a number.")) from None
        return int(number) if number.is_integer() else number
    if t == FieldType.DATE:
        parsed = value if isinstance(value, date) else parse_date(str(value))
        if parsed is None:
            raise ValidationError(_("Enter a valid date (YYYY-MM-DD)."))
        return parsed.isoformat()
    if t == FieldType.YES_NO:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"yes", "true", "1", "y", "نعم"}:
            return True
        if text in {"no", "false", "0", "n", "لا"}:
            return False
        raise ValidationError(_("Choose yes or no."))
    if t == FieldType.CHOICE:
        if value not in field.options:
            raise ValidationError(_("“%(value)s” is not one of the options.") % {"value": value})
        return value
    if t == FieldType.MULTI:
        values = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
        bad = [v for v in values if v not in field.options]
        if bad:
            raise ValidationError(_("Not an option: %(list)s") % {"list": ", ".join(bad)})
        return values
    return value


def active_fields(organization, entity: str):
    return (
        CustomField.objects.for_entity(organization, entity)
        .filter(is_active=True)
        .prefetch_related("event_types")
        .order_by("order", "id")
    )


def fields_for(record, organization, entity: str) -> list[CustomField]:
    """The active fields of `entity` that apply to this record (e.g. the event's type)."""
    return [f for f in active_fields(organization, entity) if f.applies(record)]


def clean_data(record, organization, entity: str, data: dict, *, partial=True, fill_defaults=False) -> dict:
    """
    Validate `data` (key -> value) against the fields that apply to `record`. With `partial`, keys
    not sent keep their stored value. `fill_defaults` gives empty fields their default (new records).
    """
    if not isinstance(data, dict):
        raise ValidationError({"custom_data": [_("Send an object of field values.")]})
    fields = {f.key: f for f in fields_for(record, organization, entity)}
    cleaned = dict(record.custom_data or {}) if partial else {}
    errors = {}
    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            errors[key] = [_("Unknown field.")]
            continue
        try:
            cleaned[key] = clean_value(field, value)
        except ValidationError as exc:
            errors[key] = exc.detail if isinstance(exc.detail, list) else [exc.detail]
    if fill_defaults:
        for key, field in fields.items():
            if key not in data and cleaned.get(key) in EMPTY and field.default not in EMPTY:
                cleaned[key] = field.default
    for key, field in fields.items():
        if field.required and cleaned.get(key) in EMPTY:
            errors.setdefault(key, [_("This field is required.")])
    if errors:
        raise ValidationError({"custom_data": errors})
    return {k: v for k, v in cleaned.items() if v is not None}


def value_or_default(field: CustomField, record):
    """(value, source): the record's value, else the field's default."""
    value = (record.custom_data or {}).get(field.key)
    if value not in EMPTY:
        return value, "record"
    if field.default not in EMPTY:
        return field.default, "default"
    return None, "empty"


def display(field: CustomField, value, lang: str) -> str:
    """A stored value as text for documents and family views."""
    if value in EMPTY:
        return ""
    t = field.field_type
    if t == FieldType.TEXT:
        return pick(localized_text(value), lang)
    if t == FieldType.YES_NO:
        pair = YES_NO.get(bool(value) if isinstance(value, bool) else value)
        return (pair[1] if lang == "ar" else pair[0]) if pair else str(value)
    if t == FieldType.CHOICE:
        return option_label(field, value, lang)
    if t == FieldType.MULTI and isinstance(value, list):
        return ("، " if lang == "ar" else ", ").join(option_label(field, v, lang) for v in value)
    if t == FieldType.DATE:
        parsed = parse_date(str(value))
        if parsed:
            with translation.override(lang):
                return formats.date_format(parsed, "j F Y")
    if t == FieldType.NUMBER and isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
