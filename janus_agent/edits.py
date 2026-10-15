"""Edit operations proposed by the model, validated and applied by the agent.

The model never writes a file. It proposes operations; each one is checked against the
task's allowed files, the path rules, the secret rules and the test guard, then applied
in memory. Nothing touches the disk here.
"""

import difflib
import re
from dataclasses import dataclass, field

from janus_agent.security import paths, test_guard
from janus_agent.security.redact import Redactor, has_placeholder

OPS = ["replace", "create", "env_add", "env_remove"]
DEFAULT_ENV_COMMENT = "set in the secret manager"
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*\|\s?")

EDIT_SCHEMA = {
    "type": "object",
    "required": ["edits"],
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["op", "file"],
                "properties": {
                    "op": {"type": "string", "enum": OPS},
                    "file": {"type": "string", "minLength": 1},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "content": {"type": "string"},
                    "key": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
    },
}

REQUIRED_FIELDS = {
    "replace": ("old", "new"),
    "create": ("content",),
    "env_add": ("key",),
    "env_remove": ("key",),
}


def check_shape(reply: dict) -> list:
    """Per-operation fields, checked before anything is applied (errors go back to the model)."""
    errors = []
    for i, edit in enumerate(reply.get("edits", [])):
        for name in REQUIRED_FIELDS[edit["op"]]:
            if name not in edit:
                errors.append(f"$.edits[{i}]: op '{edit['op']}' needs the field '{name}'")
        if edit["op"] == "replace" and edit.get("old", "x") == "":
            errors.append(f"$.edits[{i}]: 'old' must not be empty")
        if edit["op"] in ("env_add", "env_remove") and "key" in edit and not ENV_KEY.match(edit["key"]):
            errors.append(f"$.edits[{i}]: invalid env var name {edit['key']!r}")
    return errors


@dataclass
class Rejection:
    task: str
    edit: dict
    reason: str


@dataclass
class FileState:
    """In-memory view of the files a round touches: rel -> text (None = does not exist)."""
    root: object
    original: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)

    def get(self, rel: str):
        if rel not in self.current:
            path = paths.resolve(self.root, rel)
            text = path.read_text(encoding="utf-8") if path.is_file() else None
            self.original[rel] = text
            self.current[rel] = text
        return self.current[rel]

    def set(self, rel: str, text: str) -> None:
        self.get(rel)
        self.current[rel] = text

    def changed(self) -> dict:
        return {rel: (self.original[rel], text) for rel, text in self.current.items()
                if text != self.original[rel]}


def _strip_line_numbers(text: str) -> str:
    return "\n".join(LINE_NUMBER_PREFIX.sub("", line, count=1) for line in text.split("\n"))


def _find_block(text: str, old: str):
    """Locate old in text: exact first, then line by line ignoring trailing spaces."""
    count = text.count(old)
    if count == 1:
        start = text.index(old)
        return start, start + len(old)
    if count > 1:
        raise ValueError(f"the 'old' text appears {count} times; quote more lines to make it unique")
    old_lines = [line.rstrip() for line in old.strip("\n").split("\n")]
    lines = text.split("\n")
    matches = [i for i in range(len(lines) - len(old_lines) + 1)
               if [line.rstrip() for line in lines[i:i + len(old_lines)]] == old_lines]
    if len(matches) != 1:
        raise ValueError("the 'old' text was not found in the file" if not matches else
                         f"the 'old' text appears {len(matches)} times; quote more lines")
    i = matches[0]
    start = sum(len(line) + 1 for line in lines[:i])
    end = start + len("\n".join(lines[i:i + len(old_lines)]))
    return start, end


def _replace(text: str, old: str, new: str) -> str:
    for candidate in (old, _strip_line_numbers(old)):
        try:
            start, end = _find_block(text, candidate)
        except ValueError as exc:
            error = exc
            continue
        return text[:start] + new + text[end:]
    raise error


def _env_keys(text: str) -> set:
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            keys.add(key[len("export "):].strip() if key.startswith("export ") else key)
    return keys


def apply_one(state: FileState, edit: dict, redactor: Redactor) -> None:
    """Apply one edit to the in-memory state, or raise ValueError with the reason."""
    raw = edit["file"].strip()
    rel = paths.check_rel(raw[2:] if raw.startswith("./") else raw)
    edit["file"] = rel
    op = edit["op"]
    text = state.get(rel)
    for name in ("new", "content", "comment"):
        if has_placeholder(edit.get(name, "")):
            raise ValueError(f"'{name}' contains a redacted secret placeholder")
        redactor.assert_clean(edit.get(name, "") or "")

    if op == "replace":
        if text is None:
            raise ValueError("the file does not exist")
        old = redactor.restore(edit["old"])
        new_text = _replace(text, old, edit["new"])
    elif op == "create":
        if text is not None:
            raise ValueError("the file already exists; use replace")
        new_text = edit["content"] if edit["content"].endswith("\n") else edit["content"] + "\n"
    elif op == "env_add":
        if not paths.is_env_file(rel):
            raise ValueError("env_add only works on .env.example and *.env deploy files; to give tests "
                             "a value, create or replace tests/conftest.py with os.environ.setdefault(...)")
        key = edit["key"]
        comment = " ".join((edit.get("comment") or DEFAULT_ENV_COMMENT).split())[:100]
        comment = comment.replace("=", " ") or DEFAULT_ENV_COMMENT
        base = text or ""
        if key in _env_keys(base):
            return  # already there: nothing to do
        if base and not base.endswith("\n"):
            base += "\n"
        new_text = f"{base}{key}=  # {comment}\n"
    elif op == "env_remove":
        if not rel.endswith(".env.example"):
            raise ValueError("env_remove only works on .env.example")
        if text is None:
            raise ValueError("the file does not exist")
        kept = [line for line in text.splitlines(keepends=True)
                if line.split("=", 1)[0].strip() != edit["key"]]
        new_text = "".join(kept)
    else:
        raise ValueError(f"unknown op {op}")

    problems = test_guard.check(rel, state.original.get(rel), new_text)
    if problems:
        raise ValueError("; ".join(problems))
    state.set(rel, new_text)


def file_matches(rel: str, allowed: set, prefixes: tuple) -> bool:
    return rel in allowed or any(rel.startswith(p) for p in prefixes)


def unified_diff(changes: dict) -> str:
    out = []
    for rel in sorted(changes):
        before, after = changes[rel]
        out.extend(difflib.unified_diff(
            (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
            fromfile=f"a/{rel}" if before is not None else "/dev/null", tofile=f"b/{rel}"))
    text = "".join(out)
    return text if not text or text.endswith("\n") else text + "\n"
