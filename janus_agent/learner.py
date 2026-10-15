"""learn: turn each fix of the last applied check into a rule and a dated decision line."""

import json
from pathlib import Path

from janus_agent import memory
from janus_agent.config import Config
from janus_agent.context import finding_view
from janus_agent.costs import CostTracker
from janus_agent.edits import unified_diff
from janus_agent.llm.base import ToolSpec
from janus_agent.llm.structured import StructuredCaller
from janus_agent.security.redact import Redactor
from janus_agent.subagents import fix_targets, prompt

LEARN_SCHEMA = {
    "type": "object",
    "required": ["rules", "decisions"],
    "properties": {
        "rules": {"type": "array", "items": {
            "type": "object", "required": ["title", "body"],
            "properties": {"title": {"type": "string", "minLength": 3, "maxLength": 120},
                           "body": {"type": "string", "minLength": 10, "maxLength": 800}}}},
        "decisions": {"type": "array", "items": {
            "type": "object", "required": ["index", "why"],
            "properties": {"index": {"type": "integer"}, "why": {"type": "string"}}}},
    },
}


class LearnError(Exception):
    pass


def _key(f: dict) -> tuple:
    return (f.get("scanner"), f.get("type"), f.get("var"), f.get("file"), f.get("config"), f.get("claim"))


def fixed_findings(run: dict) -> list:
    after = {_key(f) for f in run["findings_after"]}
    return [f for f in run["findings_before"] if _key(f) not in after]


def load_last_run(project: Path) -> tuple:
    path = Path(project) / ".janus" / "runs" / "last.json"
    if not path.is_file():
        raise LearnError("no check run found: run `janus-agent check` first")
    run = json.loads(path.read_text(encoding="utf-8"))
    if not run.get("applied"):
        raise LearnError("the last check was not applied: nothing to learn from")
    if run.get("learned"):
        raise LearnError("the last check was already learned")
    return path, run


def run_learn(project: Path, config: Config, provider, out=print, apply=False, confirm=None) -> bool:
    project = Path(project).resolve()
    run_path, run = load_last_run(project)
    fixed = fixed_findings(run)
    if not fixed:
        raise LearnError("the last check fixed nothing")

    redactor = Redactor()
    rules_path = memory.rules_file(project, config.rules_path)
    current_rules = rules_path.read_text(encoding="utf-8") if rules_path.is_file() else None
    views = [finding_view(f, redactor) for f in fixed]
    user = ("Fixed findings:\n" + "\n".join(f"{i}: {json.dumps(v)}" for i, v in enumerate(views))
            + "\n\nExisting rules:\n" + (current_rules or "(none)"))
    redactor.assert_clean(user)

    costs = CostTracker(config.pricing)
    caller = StructuredCaller(provider, config.model.tool_calling, config.check.max_attempts,
                              on_usage=costs.add, log=out)
    reply = caller.ask(prompt("learn"), user,
                       ToolSpec("submit_lessons", "Rules and decision reasons.", LEARN_SCHEMA), tag="learn")

    why = {d["index"]: d["why"] for d in reply["decisions"]}
    changed = set(run.get("changed_files", []))
    rows = []
    for i, f in enumerate(fixed):
        files = [t for t in fix_targets(f, project) if t in changed] or sorted(changed)
        rows.append({"drift": views[i].get("detail", f.get("type")),
                     "change": "updated " + ", ".join(f"`{t}`" for t in files),
                     "why": why.get(i, "")})

    new_rules, added = memory.add_rules(current_rules, reply["rules"])
    decisions_path = project / "DECISIONS.md"
    current_decisions = decisions_path.read_text(encoding="utf-8") if decisions_path.is_file() else None
    new_decisions = memory.add_decisions(current_decisions, rows)

    changes = {}
    if added:
        changes[str(rules_path.relative_to(project))] = (current_rules, new_rules)
    changes["DECISIONS.md"] = (current_decisions, new_decisions)
    diff = unified_diff(changes)
    out(f"Rules added: {len(added)} ({', '.join(added) or 'none new'}). Decision lines: {len(rows)}.")
    out(diff)
    out(costs.render())
    if not (apply or (confirm and confirm(diff))):
        out("Dry run: nothing was written. Use --apply to write without asking.")
        return False
    for rel, (_, text) in changes.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run["learned"] = True
    run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
    out(f"Written: {', '.join(changes)}.")
    return True
