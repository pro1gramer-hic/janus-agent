"""Project memory: learned rules (injected in every model call) and the decision log."""

import datetime
import re
from pathlib import Path

RULES_HEADER = ("# Rules learned by Janus Agent\n\n"
                "Written by `janus-agent learn` after real fixes in this project. "
                "They are sent to the model on every call.\n")
DECISIONS_HEADER = "# Decision log\n\n| Date | Drift | Change | Why |\n|------|-------|--------|-----|\n"
MAX_RULES_CHARS = 6000


def rules_file(project: Path, rules_path: str) -> Path:
    return Path(project) / rules_path


def load_rules(project: Path, rules_path: str) -> str:
    path = rules_file(project, rules_path)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > MAX_RULES_CHARS:
        text = text[:MAX_RULES_CHARS] + "\n(rules truncated)"
    return text


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def existing_titles(text: str) -> set:
    return {_norm(m.group(1)) for m in re.finditer(r"^## (.+)$", text, re.M)}


def add_rules(current: str | None, rules: list) -> tuple:
    """Return (new text, rules actually added). Rules with a known title are skipped."""
    text = current if current else RULES_HEADER
    titles = existing_titles(text)
    added = []
    for rule in rules:
        title = " ".join(rule["title"].split())
        if not title or _norm(title) in titles:
            continue
        titles.add(_norm(title))
        body = rule["body"].strip()
        text = text.rstrip("\n") + f"\n\n## {title}\n{body}\n"
        added.append(title)
    return text, added


def today() -> str:
    """The real system date, never a guess."""
    return datetime.date.today().isoformat()


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def add_decisions(current: str | None, rows: list, date: str | None = None) -> str:
    date = date or today()
    text = current if current else DECISIONS_HEADER
    if not text.endswith("\n"):
        text += "\n"
    for row in rows:
        text += f"| {date} | {_cell(row['drift'])} | {_cell(row['change'])} | {_cell(row['why'])} |\n"
    return text
