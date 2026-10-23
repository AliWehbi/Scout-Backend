"""
Checks an uploaded .docx before anything else parses it.

The file is only ever read in memory with `zipfile` (never extracted), and rejected when it:
is not a plain Word document (macro-enabled, template, encrypted); is a zip bomb; carries
macros, ActiveX controls or embedded OLE objects; points to outside resources other than
plain hyperlinks (remote templates, frames, tracking images); or declares XML entities (XXE).
"""

import re
import zipfile
from io import BytesIO

from django.utils.translation import gettext as _
from lxml import etree
from rest_framework.exceptions import ValidationError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 300
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024
MAX_RATIO = 100
RATIO_CHECK_FROM = 1024 * 1024  # small parts may compress very well, that's harmless

DOCUMENT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
WORKBOOK_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# Relationship types (last path segment) that pull in active or outside content.
FORBIDDEN_REL_TYPES = {
    "attachedTemplate",
    "oleObject",
    "package",
    "control",
    "activeXControl",
    "activeXControlBinary",
    "aFChunk",
    "subDocument",
    "vbaProject",
    "frame",
    # Excel: links to other workbooks and live data connections.
    "externalLink",
    "connections",
    "queryTable",
}
FORBIDDEN_PREFIXES = ("word/activex/", "customui/", "xl/activex/", "xl/externallinks/", "xl/ctrlprops/")
# Excel parts that pull in outside data or run code.
FORBIDDEN_XLSX_PARTS = ("xl/connections.xml",)
ENTITY_RE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)

_parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False)


def _reject(message):
    raise ValidationError({"file": [message]})


def _check_entries(zf: zipfile.ZipFile, embeddings: str = "word/embeddings/") -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        _reject(_("This document has too many parts."))
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith("/") or "\\" in name or ".." in name.split("/"):
            _reject(_("This file is not a valid Word document."))
        if info.flag_bits & 0x1:
            _reject(_("Password-protected documents can't be used as templates."))
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            _reject(_("This document is too large once unpacked."))
        if info.file_size > RATIO_CHECK_FROM and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            _reject(_("This document is too large once unpacked."))
        lower = name.lower()
        if "vbaproject" in lower or "vbadata" in lower or lower.startswith(FORBIDDEN_PREFIXES):
            _reject(_("Documents with macros or controls can't be used as templates."))
        # Charts keep their data in an embedded .xlsx, which is fine; anything else (OLE objects,
        # embedded documents or programs) is not.
        if lower.startswith(embeddings) and not lower.endswith(".xlsx"):
            _reject(_("Documents with embedded objects can't be used as templates."))
    return infos


def _check_xml(zf: zipfile.ZipFile, infos) -> None:
    names = {i.filename for i in infos}
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        _reject(_("This file is not a valid Word document."))

    for info in infos:
        lower = info.filename.lower()
        if not (lower.endswith(".xml") or lower.endswith(".rels")):
            continue
        raw = zf.read(info)
        # Word always writes UTF-8; other encodings would slip past the entity check below.
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or ENTITY_RE.search(raw):
            _reject(_("This file is not a valid Word document."))
        if info.filename == "[Content_Types].xml":
            if b"macroEnabled" in raw or b"vbaProject" in raw:
                _reject(_("Documents with macros or controls can't be used as templates."))
            if DOCUMENT_MAIN.encode() not in raw:
                _reject(_("Save the template as a Word document (.docx), not a template or macro file."))
        if lower.endswith(".rels"):
            _check_rels(raw)


def _check_rels(raw: bytes) -> None:
    try:
        root = etree.fromstring(raw, _parser)
    except etree.XMLSyntaxError:
        _reject(_("This file is not a valid Word document."))
    for rel in root.iter(f"{REL_NS}Relationship"):
        rel_type = (rel.get("Type") or "").rsplit("/", 1)[-1]
        if rel_type in FORBIDDEN_REL_TYPES:
            _reject(_("Documents with macros, controls, embedded objects or linked templates can't be used."))
        if (rel.get("TargetMode") or "").lower() == "external" and rel_type != "hyperlink":
            _reject(_("This document loads content from the internet. Remove linked images or files and try again."))


def inspect_docx(data: bytes) -> None:
    """Raises a ValidationError on the `file` field when the upload isn't a safe .docx."""
    if len(data) > MAX_UPLOAD_BYTES:
        _reject(_("The template must be 5 MB or smaller."))
    if not zipfile.is_zipfile(BytesIO(data)):
        _reject(_("This file is not a valid Word document."))
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = _check_entries(zf)
            _check_xml(zf, infos)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, NotImplementedError):
        _reject(_("This file is not a valid Word document."))


def _check_workbook_xml(zf: zipfile.ZipFile, infos) -> None:
    names = {i.filename for i in infos}
    if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
        _reject(_("This file is not a valid Excel workbook."))
    for info in infos:
        lower = info.filename.lower()
        if lower in FORBIDDEN_XLSX_PARTS or lower.startswith("xl/querytables/"):
            _reject(_("Workbooks with data connections or links to other files can't be used as templates."))
        if not (lower.endswith(".xml") or lower.endswith(".rels")):
            continue
        raw = zf.read(info)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or ENTITY_RE.search(raw):
            _reject(_("This file is not a valid Excel workbook."))
        if info.filename == "[Content_Types].xml":
            if b"macroEnabled" in raw or b"vbaProject" in raw:
                _reject(_("Workbooks with macros or controls can't be used as templates."))
            if WORKBOOK_MAIN.encode() not in raw:
                _reject(_("Save the template as an Excel workbook (.xlsx), not a template or macro file."))
        if lower.endswith(".rels"):
            _check_rels(raw)


def inspect_xlsx(data: bytes) -> None:
    """Raises a ValidationError on the `file` field when the upload isn't a safe .xlsx."""
    if len(data) > MAX_UPLOAD_BYTES:
        _reject(_("The template must be 5 MB or smaller."))
    if not zipfile.is_zipfile(BytesIO(data)):
        _reject(_("This file is not a valid Excel workbook."))
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = _check_entries(zf, embeddings="xl/embeddings/")
            _check_workbook_xml(zf, infos)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, NotImplementedError):
        _reject(_("This file is not a valid Excel workbook."))


def inspect(data: bytes, fmt: str) -> None:
    inspect_xlsx(data) if fmt == "xlsx" else inspect_docx(data)
