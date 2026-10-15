"""Mask anything that looks like a secret before text leaves the machine.

Each distinct secret becomes a stable placeholder such as <<REDACTED_1>>. The model may
quote a placeholder in the "old" side of an edit to point at a line; the agent maps it
back to the real text locally. A placeholder in new content is refused, so a secret can
never be copied somewhere else.
"""

import math
import re
import threading
from collections import Counter

from janus_agent.scanners.scan_secrets import KNOWN_FORMATS, _is_secret_name, _looks_like_placeholder

PLACEHOLDER = re.compile(r"<<REDACTED_\d+>>")

# NAME = "value", NAME: value, "name": "value", NAME=value (env files)
NAMED_VALUE = re.compile(
    r"""(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]\s*(?P<q>["']?)(?P<value>[^"'\s#,;)}]+)(?P=q)"""
)
LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


class LeakError(Exception):
    pass


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _looks_random(token: str) -> bool:
    """Long, mixed letters and digits, high entropy: an API key or a hash."""
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return has_digit and has_alpha and _entropy(token) >= 4.0


class Redactor:
    def __init__(self, known_secrets=()):
        self._lock = threading.Lock()
        self._by_value = {}
        self._by_placeholder = {}
        self.known = {s for s in known_secrets if s and len(s) >= 3}

    def add_known(self, values) -> None:
        with self._lock:
            self.known.update(v for v in values if v and len(v) >= 3)

    def _placeholder(self, value: str) -> str:
        with self._lock:
            if value not in self._by_value:
                tag = f"<<REDACTED_{len(self._by_value) + 1}>>"
                self._by_value[value] = tag
                self._by_placeholder[tag] = value
            return self._by_value[value]

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in sorted(self.known, key=len, reverse=True):
            if value in text:
                text = text.replace(value, self._placeholder(value))
        for _, pattern in KNOWN_FORMATS:
            text = pattern.sub(lambda m: self._placeholder(m.group(0)), text)

        def named(match):
            value = match.group("value")
            if (PLACEHOLDER.fullmatch(value) or not _is_secret_name(match.group("name"))
                    or _looks_like_placeholder(value) or _is_code_reference(value)):
                return match.group(0)
            start = match.start("value") - match.start(0)
            end = match.end("value") - match.start(0)
            whole = match.group(0)
            return whole[:start] + self._placeholder(value) + whole[end:]

        text = NAMED_VALUE.sub(named, text)
        text = LONG_TOKEN.sub(
            lambda m: self._placeholder(m.group(0)) if _looks_random(m.group(0)) else m.group(0), text)
        return text

    def redact_env_file(self, text: str) -> str:
        """Env files: keep the keys and comments, hide every non-empty value."""
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key, _, rest = line.partition("=")
            value, hash_, comment = rest.partition("#")
            if value.strip():
                lines.append(f"{key}={self._placeholder(value.strip())}"
                             + (f"  #{comment}" if hash_ else ""))
            else:
                lines.append(line)
        return "\n".join(lines)

    def restore(self, text: str) -> str:
        with self._lock:
            mapping = dict(self._by_placeholder)
        return PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)

    def assert_clean(self, text: str) -> None:
        """Last line of defense before a request is sent."""
        for value in self.known:
            if value in text:
                raise LeakError("a known secret value is about to be sent to the model")
        for kind, pattern in KNOWN_FORMATS:
            if pattern.search(text):
                raise LeakError(f"a {kind} is about to be sent to the model")


def has_placeholder(text: str) -> bool:
    return bool(text) and bool(PLACEHOLDER.search(text))


def _is_code_reference(value: str) -> bool:
    """os.environ["X"], settings.token, get_secret(...): code, not a literal secret."""
    return bool(re.match(r"^(os\.|environ|settings\.|config\.|self\.|get|None$|True$|False$)", value))


def secret_values_from_findings(root, findings) -> set:
    """The raw values behind the secrets scanner's findings, read locally, never sent."""
    from pathlib import Path

    values = set()
    for f in findings:
        if f.get("scanner") != "secrets" or not f.get("line"):
            continue
        path = Path(root) / f["file"]
        try:
            line = path.read_text(encoding="utf-8").splitlines()[f["line"] - 1]
        except (OSError, IndexError, UnicodeDecodeError):
            continue
        for _, pattern in KNOWN_FORMATS:
            values.update(m.group(0) for m in pattern.finditer(line))
        for match in NAMED_VALUE.finditer(line):
            if _is_secret_name(match.group("name")) and not _looks_like_placeholder(match.group("value")):
                values.add(match.group("value"))
    return values
