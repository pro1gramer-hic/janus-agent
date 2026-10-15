"""Build what the model sees: redacted findings and the cited lines, never whole files."""

from pathlib import Path

from janus_agent.scanners.scan_docs import ENDPOINT
from janus_agent.security import paths
from janus_agent.security.redact import Redactor

FINDING_FIELDS = ("scanner", "type", "severity", "var", "file", "line", "config", "claim", "truth",
                  "detail", "suggested_env_var")
MAX_ENV_LINES = 80


def finding_view(finding: dict, redactor: Redactor) -> dict:
    """The finding without masked_value (even 4 characters are a leak), every string redacted."""
    view = {}
    for key in FINDING_FIELDS:
        value = finding.get(key)
        if value is None:
            continue
        view[key] = redactor.redact(value) if isinstance(value, str) else value
    return view


def _lines(root: Path, rel: str):
    try:
        return paths.read_text(root, rel).splitlines()
    except (paths.PathError, OSError, UnicodeDecodeError):
        return None


def snippet(root: Path, rel: str, line: int | None, radius: int, redactor: Redactor) -> str | None:
    """Numbered lines around `line` ("12 | text"), redacted."""
    lines = _lines(root, rel)
    if lines is None:
        return None
    if paths.is_env_file(rel):
        return env_view(root, rel, redactor)
    if not line:
        line, radius = 1, min(radius, 10)
    start, end = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(end))
    return "\n".join(f"{n:>{width}} | {redactor.redact(lines[n - 1])}" for n in range(start, end + 1))


def numbered(root: Path, rel: str, numbers, redactor: Redactor) -> str | None:
    lines = _lines(root, rel)
    if lines is None:
        return None
    width = len(str(len(lines)))
    return "\n".join(f"{n:>{width}} | {redactor.redact(lines[n - 1])}"
                     for n in sorted(set(numbers)) if 1 <= n <= len(lines))


def env_view(root: Path, rel: str, redactor: Redactor) -> str:
    """An env file shown as keys only: values are always hidden."""
    try:
        text = paths.read_text(root, rel)
    except (paths.PathError, OSError, UnicodeDecodeError):
        return "(file does not exist yet)"
    lines = redactor.redact_env_file(text).splitlines()[:MAX_ENV_LINES]
    return "\n".join(lines) if lines else "(empty file)"


def readme_endpoint_lines(root: Path, readme: str) -> list:
    lines = _lines(root, readme) or []
    return [n for n, line in enumerate(lines, start=1) if ENDPOINT.search(line)]


def block(title: str, body: str | None) -> str:
    return f"--- {title} ---\n{body if body is not None else '(not available)'}\n"
