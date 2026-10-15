"""The three sub-agents of a check: config_secrets, docs, tests.

Findings are routed by the file the fix will touch, not by the file where they were seen
(a missing variable is seen in app code but fixed in .env.example). Each task gets its own
context: only its findings, its snippets and its allowed files.
"""

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from janus_agent import context
from janus_agent.edits import EDIT_SCHEMA, check_shape
from janus_agent.llm.base import ToolSpec
from janus_agent.security.redact import Redactor
from janus_agent.security.test_guard import is_test_file

TASK_ORDER = ["config_secrets", "docs", "tests"]
EDIT_TOOL = ToolSpec(
    name="submit_edits",
    description="Submit the file edits that fix the findings.",
    schema=EDIT_SCHEMA,
)


def prompt(name: str) -> str:
    return resources.files("janus_agent.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


@dataclass
class Task:
    name: str
    findings: list = field(default_factory=list)
    allowed: set = field(default_factory=set)
    prefixes: tuple = ()
    notes: list = field(default_factory=list)  # test output, refused edits from the last round

    test_output: str | None = None

    def is_empty(self) -> bool:
        """Notes alone (refused edits) never keep a task alive: it needs findings or failing tests."""
        return not self.findings and not self.test_output


def _readme_name(root: Path) -> str:
    for name in ("README.md", "readme.md", "Readme.md"):
        if (root / name).exists():
            return name
    return "README.md"


def fix_targets(finding: dict, root: Path) -> list:
    """The files a finding's fix will edit."""
    scanner, kind = finding.get("scanner"), finding.get("type")
    if scanner == "config":
        if kind == "missing_in_deploy_config":
            return [finding["config"]]
        return [".env.example"]
    if scanner in ("secrets", "agent"):
        return [finding["file"]]
    if scanner == "docs":
        return [finding["file"] if finding["file"].lower().endswith(".md") else _readme_name(root)]
    return [finding.get("file")]


def route(finding: dict, root: Path) -> str:
    scanner = finding.get("scanner")
    if scanner == "docs":
        return "docs"
    if scanner in ("secrets", "agent") and is_test_file(finding.get("file", "")):
        return "tests"
    return "config_secrets"


def split(findings: list, root: Path, deploy_configs: list, test_output: str | None,
          strict_env_vars: list, notes: dict | None = None) -> tuple:
    """Return (tasks, held_back findings)."""
    tasks = {name: Task(name) for name in TASK_ORDER}
    has_secrets = any(f.get("scanner") == "secrets" for f in findings)
    held = []
    for f in findings:
        # Moving the secrets makes this claim true: only fix it if it survives the re-scan
        if f.get("type") == "security_claim_false" and has_secrets:
            held.append(f)
            continue
        task = tasks[route(f, root)]
        task.findings.append(f)
        task.allowed.update(t for t in fix_targets(f, root) if t)

    config = tasks["config_secrets"]
    if config.findings:
        config.allowed.update([".env.example", *deploy_configs])
        if any(f["scanner"] == "secrets" for f in config.findings):
            config.notes.append("Deploy configs: " + (", ".join(deploy_configs) or "none"))
    tests = tasks["tests"]
    if test_output:
        tests.test_output = test_output
        tests.notes.append("Variables read with os.environ[...] (required at import time): "
                           + (", ".join(strict_env_vars) or "none"))
        tests.notes.append("Test output (last lines):\n" + test_output)
    tests.prefixes = ("tests/",)
    tests.allowed.add("conftest.py")

    for name, extra in (notes or {}).items():
        if name in tasks and extra:
            tasks[name].notes.extend(extra)
    return [tasks[name] for name in TASK_ORDER if not tasks[name].is_empty()], held


def detect_overlaps(tasks: list) -> list:
    """Files that two tasks would both be allowed to edit."""
    seen, overlaps = {}, []
    for task in tasks:
        for rel in task.allowed:
            if rel in seen and seen[rel] != task.name:
                overlaps.append((rel, seen[rel], task.name))
            seen.setdefault(rel, task.name)
    return overlaps


def build_user_message(task: Task, root: Path, redactor: Redactor, radius: int) -> str:
    parts = [f"Files you may edit: {', '.join(sorted(task.allowed))}"
             + (f" and new files under {', '.join(task.prefixes)}" if task.prefixes else ""), ""]
    shown_env = set()
    for i, finding in enumerate(task.findings, start=1):
        parts.append(f"Finding {i}: " + str(context.finding_view(finding, redactor)))
        rel, line = finding.get("file"), finding.get("line")
        if rel and rel not in shown_env:
            parts.append(context.block(f"{rel}", context.snippet(root, rel, line, radius, redactor)))
        if finding.get("type") == "undocumented_endpoint":
            readme = _readme_name(root)
            lines = context.readme_endpoint_lines(root, readme)
            parts.append(context.block(f"{readme} (endpoint lines)",
                                       context.numbered(root, readme, lines, redactor)))
        for target in fix_targets(finding, root):
            if target and target != rel and target.endswith((".env", ".env.example")) and target not in shown_env:
                shown_env.add(target)
                parts.append(context.block(f"{target} (keys only, values hidden)",
                                           context.env_view(root, target, redactor)))
    if task.name == "config_secrets":
        for rel in sorted(r for r in task.allowed if r.endswith((".env", ".env.example"))):
            if rel not in shown_env:
                shown_env.add(rel)
                parts.append(context.block(f"{rel} (keys only, values hidden)",
                                           context.env_view(root, rel, redactor)))
    if task.name == "tests":
        conftest = root / "tests" / "conftest.py"
        if conftest.is_file():
            parts.append(context.block("tests/conftest.py (exists: use replace to extend it)",
                                       context.snippet(root, "tests/conftest.py", 1, 40, redactor)))
        else:
            parts.append("tests/conftest.py does not exist yet: use the create operation.")
    for note in task.notes:
        parts.append(redactor.redact(note))
    return "\n".join(parts)


def system_prompt(task_name: str, rules: str) -> str:
    text = prompt("system")
    if rules:
        text += "\nLearned project rules (follow them):\n" + rules + "\n"
    return text + "\n" + prompt(f"task_{task_name}")


def run_task(task: Task, root: Path, caller, redactor: Redactor, rules: str, radius: int) -> dict:
    """One sub-agent: one structured call with its own context. Returns the reply."""
    user = build_user_message(task, root, redactor, radius)
    system = system_prompt(task.name, rules)
    redactor.assert_clean(system + user)
    return caller.ask(system, user, EDIT_TOOL, tag=task.name, check=check_shape)
