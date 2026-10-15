# Copyright (c) 2026 pro1gramer-hic
# Originally part of Janus (https://github.com/pro1gramer-hic/Janus-bob), MIT License.
# See NOTICE and LICENSE.
"""Scanner 5: secrets written directly in the code or in config files."""

import ast
import re
from pathlib import Path

from janus_agent.scanners.scan_config import iter_files

SECRET_NAME = re.compile(
    r"secret|passw(or)?d|pwd|token|api[_-]?key|private[_-]?key|credential|auth[_-]?key",
    re.I,
)
# Name parts that mean "this is about a secret, not the secret itself" (JWT_SECRET_HEADER, TOKEN_TTL...)
SAFE_PARTS = {"url", "uri", "endpoint", "header", "name", "field", "path", "file", "env",
              "var", "length", "type", "ttl", "expiry", "expires", "timeout", "prefix",
              # added by Janus Agent: limits such as max_tokens are not secrets
              "max", "min", "limit", "count", "size"}

KNOWN_FORMATS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password_in_url", re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@", re.I)),
    # Model provider keys (added by Janus Agent). The lookbehind avoids matching inside words.
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("groq_key", re.compile(r"(?<![A-Za-z0-9])gsk_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    ("huggingface_token", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

TEXT_SUFFIXES = [".py", ".env", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg"]
# Prose files: only known key formats are checked, never "NAME = value" lines
FORMAT_ONLY_SUFFIXES = [".md", ".txt"]
ASSIGN_LINE = re.compile(r"""^\s*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]\s*["']?([^"'\s#,]+)""")
PLACEHOLDERS = {"none", "null", "changeme", "xxx", "todo", "your-secret-here", "..."}


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    parts = set(re.split(r"[_.\-]", lower))
    return bool(SECRET_NAME.search(lower)) and not (parts & SAFE_PARTS)


def _looks_like_placeholder(value: str) -> bool:
    v = value.strip()
    return (
        not v
        or v.startswith("${")
        or (v.startswith("<") and v.endswith(">"))
        or v.lower() in PLACEHOLDERS
    )


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:4]}...({len(value)} chars)"


def _add(found: dict, rel: str, line: int, name: str, value: str, kind: str) -> None:
    key = (rel, line)
    if key in found:
        return
    in_tests = "tests" in Path(rel).parts
    severity = "critical" if kind == "private_key_block" else "medium" if in_tests else "high"
    suggested = name.upper() if kind == "named_secret" and name.isidentifier() else None
    found[key] = {
        "scanner": "secrets",
        "type": kind,
        "severity": severity,
        "var": name,
        "file": rel,
        "line": line,
        "masked_value": _mask(value),
        "suggested_env_var": suggested,
        "detail": f"{name} is hardcoded ({_mask(value)}): move it to an environment variable",
    }


def _scan_python(path: Path, rel: str, found: dict) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    pairs.append((target.id, node.value, node.lineno))
                elif isinstance(target, ast.Attribute):
                    pairs.append((target.attr, node.value, node.lineno))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            pairs.append((node.target.id, node.value, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg:
            pairs.append((node.arg, node.value, node.value.lineno))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    pairs.append((key.value, value, value.lineno))
        for name, value, line in pairs:
            if (
                _is_secret_name(name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and not _looks_like_placeholder(value.value)
            ):
                _add(found, rel, line, name, value.value, "named_secret")


def _scan_text(path: Path, rel: str, found: dict, check_assignments: bool) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in KNOWN_FORMATS:
            match = pattern.search(line)
            if match:
                _add(found, rel, number, kind, match.group(0), kind)
        if check_assignments:
            match = ASSIGN_LINE.match(line)
            if match and _is_secret_name(match.group(1)) and not _looks_like_placeholder(match.group(2)):
                _add(found, rel, number, match.group(1), match.group(2), "named_secret")


def scan(root) -> dict:
    root = Path(root)
    found = {}
    files_scanned = 0
    for suffix in TEXT_SUFFIXES:
        for path in iter_files(root, suffix):
            if path.name == ".env":
                continue  # the real .env is never read
            files_scanned += 1
            rel = path.relative_to(root).as_posix()
            if suffix == ".py":
                _scan_python(path, rel, found)
                _scan_text(path, rel, found, check_assignments=False)
            else:
                _scan_text(path, rel, found, check_assignments=True)
    for suffix in FORMAT_ONLY_SUFFIXES:
        for path in iter_files(root, suffix):
            files_scanned += 1
            _scan_text(path, path.relative_to(root).as_posix(), found, check_assignments=False)

    findings = sorted(found.values(), key=lambda f: (f["file"], f["line"]))
    return {
        "scanner": "secrets",
        "summary": {"files_scanned": files_scanned, "secrets_found": len(findings)},
        "findings": findings,
    }
