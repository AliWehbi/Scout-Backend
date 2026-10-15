"""
Filling a Word template with data.

Templates are uploaded by organization admins, so they are treated as untrusted code:

* Jinja runs in a sandbox: no access to private attributes or Python internals, `range`
  is capped, repeating strings/lists with `*` is capped and `**` is refused.
* Values are XML-escaped (`autoescape`), so a member called `</w:t>…` can't inject Word XML.
* The context only holds plain dicts, lists and strings (see `registry.py`), never models.
* Rendering runs in a separate, short-lived process (`python -m apps.documents.rendering`)
  with memory and CPU limits and a wall-clock timeout, so a hostile template (deeply nested
  loops, huge output) can't take the web worker down. It gets no secrets in its environment,
  never touches the database, and talks over stdin/stdout with JSON and raw bytes only (no
  pickle), so even a compromised render process can't run code in the web worker.

This module must not import Django: the render process runs it on its own.
"""

import base64
import io
import json
import os
import resource
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage
from jinja2 import StrictUndefined, TemplateSyntaxError, Undefined, UndefinedError
from jinja2.exceptions import SecurityError, TemplateError
from jinja2.sandbox import SandboxedEnvironment

MAX_RANGE = 1000
MAX_SEQUENCE = 100_000  # longest string/list `*` may produce
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
RENDER_TIMEOUT_SECONDS = 15
RENDER_CPU_SECONDS = 10
RENDER_MEMORY_HEADROOM = 512 * 1024 * 1024

# str methods that can blow a short string up to any size.
_UNSAFE_STR_METHODS = frozenset({"ljust", "rjust", "center", "zfill", "expandtabs", "format", "format_map"})
_UNSAFE_FILTERS = ("center", "indent", "wordwrap", "pprint", "xmlattr")


class RenderError(Exception):
    """The template could not be filled in. `message` is safe to show to the admin."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message


def _capped_range(*args):
    rng = range(*args)
    if len(rng) > MAX_RANGE:
        raise SecurityError(f"range() is limited to {MAX_RANGE} items.")
    return rng


class DocumentSandbox(SandboxedEnvironment):
    intercepted_binops = frozenset({"*", "**"})

    def __init__(self, *, strict: bool = False):
        super().__init__(undefined=StrictUndefined if strict else Undefined, autoescape=True)
        self.globals = {"range": _capped_range}
        for name in _UNSAFE_FILTERS:
            self.filters.pop(name, None)

    def call_binop(self, context, operator, left, right):
        if operator == "**":
            raise SecurityError("The ** operator is not allowed.")
        if operator == "*":
            for seq, n in ((left, right), (right, left)):
                if isinstance(seq, str | list | tuple) and isinstance(n, int) and len(seq) * n > MAX_SEQUENCE:
                    raise SecurityError("This repetition is too long.")
        return super().call_binop(context, operator, left, right)

    def is_safe_attribute(self, obj, attr, value):
        if isinstance(obj, str) and attr in _UNSAFE_STR_METHODS:
            return False
        return super().is_safe_attribute(obj, attr, value)


@dataclass
class RenderJob:
    template: bytes
    context: dict  # JSON-serializable
    logo: bytes | None = None
    # Validation mode: unknown fields are errors, and only `allowed_roots` may be used.
    strict: bool = False
    allowed_roots: frozenset[str] = field(default_factory=frozenset)

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "template": base64.b64encode(self.template).decode(),
                "context": self.context,
                "logo": base64.b64encode(self.logo).decode() if self.logo else None,
                "strict": self.strict,
                "allowed_roots": sorted(self.allowed_roots),
            }
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "RenderJob":
        data = json.loads(raw)
        return cls(
            template=base64.b64decode(data["template"]),
            context=data["context"],
            logo=base64.b64decode(data["logo"]) if data["logo"] else None,
            strict=data["strict"],
            allowed_roots=frozenset(data["allowed_roots"]),
        )


def _limit_resources():
    with open("/proc/self/statm") as f:
        current = int(f.read().split()[0]) * resource.getpagesize()
    limit = current + RENDER_MEMORY_HEADROOM
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CPU, (RENDER_CPU_SECONDS, RENDER_CPU_SECONDS + 1))


def _render(job: RenderJob) -> bytes:
    tpl = DocxTemplate(io.BytesIO(job.template))
    env = DocumentSandbox(strict=job.strict)
    if job.strict:
        unknown = sorted(tpl.get_undeclared_template_variables(env) - job.allowed_roots)
        if unknown:
            raise RenderError("unknown_field", ", ".join(unknown))
    context = dict(job.context)
    if "org" in context:
        logo = InlineImage(tpl, io.BytesIO(job.logo), height=Mm(18)) if job.logo else ""
        context["org"] = {**context["org"], "logo": logo}
    tpl.render(context, jinja_env=env, autoescape=True)
    out = io.BytesIO()
    tpl.save(out)
    data = out.getvalue()
    if len(data) > MAX_OUTPUT_BYTES:
        raise RenderError("too_large")
    return data


def _worker() -> int:
    """Entry point of the render process: a job as JSON on stdin, the .docx on stdout."""
    _limit_resources()
    try:
        data = _render(RenderJob.from_json(sys.stdin.buffer.read()))
    except RenderError as exc:
        error = (exc.code, exc.message)
    except TemplateSyntaxError as exc:
        error = ("syntax", exc.message or "")
    except UndefinedError as exc:
        error = ("unknown_field", exc.message or "")
    except SecurityError as exc:
        error = ("not_allowed", str(exc))
    except TemplateError as exc:
        error = ("template", exc.message or "")
    except MemoryError:
        error = ("too_large", "")
    except Exception:  # noqa: BLE001 - anything else in a broken file is reported the same way
        error = ("invalid", "")
    else:
        sys.stdout.buffer.write(data)
        return 0
    sys.stdout.buffer.write(json.dumps({"code": error[0], "message": error[1][:500]}).encode())
    return 2


BACKEND_DIR = Path(__file__).resolve().parents[2]


def render_docx(job: RenderJob) -> bytes:
    # Only what Python needs to start: no Django settings, secret key or database URL.
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(BACKEND_DIR), "LANG": "C.UTF-8"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "apps.documents.rendering"],
            input=job.to_json(),
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            env=env,
            cwd=BACKEND_DIR,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RenderError("timeout") from None
    if proc.returncode == 0:
        if len(proc.stdout) > MAX_OUTPUT_BYTES:
            raise RenderError("too_large")
        return proc.stdout
    if proc.returncode == 2:
        try:
            error = json.loads(proc.stdout)
            raise RenderError(str(error["code"]), str(error.get("message", ""))[:500])
        except (ValueError, KeyError, TypeError):
            pass
    # Killed by the CPU or memory limit, or crashed.
    raise RenderError("too_large" if proc.returncode < 0 else "invalid")


if __name__ == "__main__":
    sys.exit(_worker())
