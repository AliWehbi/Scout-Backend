"""Validating uploaded templates and producing documents from them."""

import hashlib
import io
import logging
import re
from datetime import date
from pathlib import Path

from django.utils.translation import gettext as _
from PIL import Image
from rest_framework.exceptions import APIException, ValidationError

from apps.events import services as event_services

from . import registry
from .models import DocumentKind, DocumentTemplate
from .rendering import RenderError, RenderJob, render_docx
from .safety import MAX_UPLOAD_BYTES, inspect_docx

logger = logging.getLogger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
MAX_LOGO_PIXELS = 600


class DocumentError(APIException):
    """A stored template that can't be used any more (the export, not the request, is at fault)."""

    status_code = 400
    default_code = "document_error"


def render_error_message(exc: RenderError) -> str:
    detail = exc.message
    messages = {
        "syntax": _("There is a mistake in a placeholder: %(detail)s"),
        "unknown_field": _("Unknown field: %(detail)s. Check the list of available fields."),
        "not_allowed": _("The template uses something that isn't allowed: %(detail)s"),
        "template": _("The template could not be filled in: %(detail)s"),
        "too_large": _("The template produces a document that is too large or takes too long."),
        "timeout": _("The template produces a document that is too large or takes too long."),
    }
    message = messages.get(exc.code, _("This file could not be read as a Word template."))
    return message % {"detail": detail} if "%(detail)s" in message else message


def _render(job: RenderJob, *, upload: bool) -> bytes:
    try:
        return render_docx(job)
    except RenderError as exc:
        message = render_error_message(exc)
        raise (ValidationError({"file": [message]}) if upload else DocumentError(message)) from None


# ------------------------------------------------------------------ uploads


def read_upload(upload, kind: str) -> bytes:
    """Returns the bytes of a safe template that fills in correctly, or raises a ValidationError."""
    if not (upload.name or "").lower().endswith(".docx"):
        raise ValidationError({"file": [_("Upload a Word document (.docx).")]})
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError({"file": [_("The template must be 5 MB or smaller.")]})
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    inspect_docx(data)
    _render(
        RenderJob(
            data,
            registry.sample_context(kind),
            strict=True,
            allowed_roots=registry.allowed_roots(kind),
        ),
        upload=True,
    )
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_filename(name: str, limit: int = 120) -> str:
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]+', " ", name or "")
    return re.sub(r"\s+", " ", name).strip()[:limit] or "document"


# ------------------------------------------------------------------ templates


def starter_bytes(kind: str, lang: str) -> bytes:
    if kind not in DocumentKind.values or lang not in registry.LANGS:
        raise ValidationError(_("Unknown document."))
    return (DEFAULTS_DIR / f"{kind}.{lang}.docx").read_bytes()


def stored_bytes(template: DocumentTemplate) -> bytes:
    """The uploaded file, checked again: it must be the exact file that passed validation."""
    with template.file.open("rb") as fh:
        data = fh.read(MAX_UPLOAD_BYTES + 1)
    if sha256(data) != template.sha256:
        logger.error("Document template %s does not match its checksum", template.pk)
        raise DocumentError(_("This template file was changed on the server. Upload it again."))
    try:
        inspect_docx(data)
    except ValidationError:
        raise DocumentError(_("This template can't be used any more. Upload it again.")) from None
    return data


def default_template(organization, kind: str) -> DocumentTemplate | None:
    return DocumentTemplate.objects.filter(organization=organization, kind=kind, is_default=True).first()


# ------------------------------------------------------------------ exporting


def allowed_kinds(service, event) -> list[str]:
    """Consent slips hold guardians' phone numbers, so they need manage rights."""
    manage = event_services.can_manage(service, event)
    kinds = [DocumentKind.EVENT_SHEET]
    if manage or event_services.can_check_in(service, event):
        kinds.append(DocumentKind.ATTENDANCE_SHEET)
    if manage:
        kinds.append(DocumentKind.CONSENT_SLIPS)
    return kinds


def logo_png(organization) -> bytes | None:
    """The org logo re-encoded as a small PNG (Word can't show every format the upload allows)."""
    if not organization.logo:
        return None
    try:
        with organization.logo.open("rb") as fh, Image.open(fh) as img:
            img.thumbnail((MAX_LOGO_PIXELS, MAX_LOGO_PIXELS))
            out = io.BytesIO()
            img.convert("RGBA").save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a broken logo shouldn't stop the export
        logger.warning("Could not read the logo of organization %s", organization.pk)
        return None


def export_event(
    event, *, kind: str, template: DocumentTemplate | None, lang: str, user, day: date | None = None
) -> bytes:
    data = stored_bytes(template) if template else starter_bytes(kind, lang)
    context = registry.build_context(kind, event, lang=lang, user=user, day=day)
    return _render(RenderJob(data, context, logo=logo_png(event.organization)), upload=False)


def export_filename(event, kind: str, lang: str, day: date | None) -> str:
    names = {
        DocumentKind.EVENT_SHEET: ("Event sheet", "ورقة النشاط"),
        DocumentKind.ATTENDANCE_SHEET: ("Attendance", "الحضور"),
        DocumentKind.CONSENT_SLIPS: ("Consent slips", "قسائم الموافقة"),
    }[kind]
    parts = [event.title, names[1] if lang == "ar" else names[0]]
    if kind == DocumentKind.ATTENDANCE_SHEET and day:
        parts.append(day.isoformat())
    return clean_filename(" - ".join(parts), limit=150) + ".docx"
