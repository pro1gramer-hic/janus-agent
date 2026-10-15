"""The check loop: scan, fix in parallel, verify with the scanners and the tests, repeat.

The model is never believed: a round succeeds only when the scanners report zero
findings and the test command exits with 0. Everything happens in a workspace copy;
the real project is written only after confirmation (or --apply).
"""

import datetime
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from janus_agent import memory, subagents
from janus_agent.config import Config
from janus_agent.costs import CostTracker
from janus_agent.edits import FileState, Rejection, apply_one, file_matches, unified_diff
from janus_agent.llm.base import ProviderError
from janus_agent.llm.structured import StructuredCaller, StructuredError
from janus_agent.scanners import run_scan, write_report
from janus_agent.scanners.scan_config import find_env_usages
from janus_agent.security.commands import CommandRefused, run_tests
from janus_agent.security.redact import LeakError, Redactor, secret_values_from_findings
from janus_agent.workspace import StaleProject, Workspace

TEST_TAIL_LINES = 40


@dataclass
class CheckResult:
    findings_before: list
    findings_after: list = field(default_factory=list)
    tests_before: bool | None = None
    tests_after: bool | None = None
    rounds: int = 0
    changes: dict = field(default_factory=dict)
    applied: bool = False
    rejections: list = field(default_factory=list)
    task_log: list = field(default_factory=list)
    costs: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def success(self) -> bool:
        return not self.findings_after and self.tests_after is not False and self.error is None


def _tail(text: str, lines: int = TEST_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _has_tests(root: Path) -> bool:
    return any(p for p in root.rglob("test_*.py") if ".venv" not in p.parts) or \
        any(p for p in root.rglob("*_test.py") if ".venv" not in p.parts)


def fail_fast_findings(root: Path, moved_secrets: list, still_hardcoded=()) -> list:
    """A moved secret must be read with os.environ["X"], never with a default or None.

    Secrets the scanner still reports as hardcoded are skipped: they were not moved yet.
    """
    usages = {}
    for usage in find_env_usages(root):
        usages.setdefault(usage["var"], []).append(usage)
    findings = []
    for var, origin in moved_secrets:
        if var in still_hardcoded:
            continue
        loose = [u for u in usages.get(var, []) if u["mode"] != "strict"]
        if loose:
            where = {"file": loose[0]["file"], "line": loose[0]["line"]}
            detail = f"{var} is read with a default or may be None; read it with os.environ[\"{var}\"]"
        elif var not in usages and (root / origin).is_file():
            where = {"file": origin, "line": None}
            detail = f"{var} was a hardcoded secret and is no longer read from the environment"
        else:
            continue
        findings.append({"scanner": "agent", "type": "secret_not_fail_fast", "severity": "high",
                         "var": var, **where, "detail": detail})
    return findings


class Checker:
    def __init__(self, project: Path, config: Config, provider, out: Callable = print,
                 confirm: Callable[[str], bool] | None = None, apply: bool = False):
        self.project = Path(project).resolve()
        self.config = config
        self.out = out
        self.confirm = confirm or (lambda diff: False)
        self.apply = apply
        self.costs = CostTracker(config.pricing)
        self.secret_values = set()
        self.caller = StructuredCaller(provider, config.model.tool_calling, config.check.max_attempts,
                                       on_usage=self.costs.add, log=out)

    # -- verification -------------------------------------------------------------------
    def _tests(self, root: Path):
        if not _has_tests(root):
            return None, ""
        try:
            result = run_tests(self.config.check.test_command, root, self.config.check.test_timeout)
        except CommandRefused as exc:
            self.out(f"tests: {exc}")
            return False, str(exc)
        return result.ok, result.output

    def _verify(self, root: Path, moved: list):
        report = run_scan(root)
        hardcoded = {f["var"] for f in report["findings"] if f["scanner"] == "secrets"}
        findings = report["findings"] + fail_fast_findings(root, moved, hardcoded)
        tests_ok, output = self._tests(root)
        return report, findings, tests_ok, output

    # -- one round ----------------------------------------------------------------------
    def _run_tasks(self, tasks, root, redactor, rules):
        replies = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {t.name: pool.submit(subagents.run_task, t, root, self.caller, redactor, rules,
                                           self.config.check.context_lines) for t in tasks}
            for name, future in futures.items():
                try:
                    replies[name] = future.result()
                except (StructuredError, ProviderError, LeakError) as exc:
                    replies[name] = exc
        return replies

    def _collect(self, tasks, replies, root, redactor):
        """Validate every edit, refuse cross-task conflicts, return the accepted changes."""
        state = FileState(root)
        owner, rejections, notes = {}, [], {}
        for task in tasks:
            reply = replies[task.name]
            if isinstance(reply, Exception):
                self.out(f"  {task.name}: failed ({reply})")
                continue
            self.out(f"  {task.name}: {len(reply.get('edits', []))} edit(s). "
                     f"{reply.get('summary', '')[:160]}")
            task_files = set()
            for edit in reply.get("edits", []):
                rel = edit.get("file", "")
                reason = None
                if not file_matches(rel.removeprefix("./"), task.allowed, task.prefixes):
                    reason = f"{rel} is not in this task's files"
                elif owner.get(rel.removeprefix("./"), task.name) != task.name:
                    reason = f"conflict: {rel} is already changed by {owner[rel.removeprefix('./')]}"
                if reason is None:
                    try:
                        apply_one(state, edit, redactor)
                        owner[edit["file"]] = task.name
                        task_files.add(edit["file"])
                        continue
                    except Exception as exc:  # noqa: BLE001 - every reason goes back to the model
                        reason = str(exc)
                rejections.append(Rejection(task.name, edit, reason))
                notes.setdefault(task.name, []).append(
                    f"An edit from the last round was refused ({edit.get('op')} {rel}): {reason}")
                self.out(f"    refused {edit.get('op')} {rel}: {reason}")
        return state.changed(), rejections, notes

    # -- the loop -----------------------------------------------------------------------
    def run(self) -> CheckResult:
        report0 = run_scan(self.project)
        write_report(self.project, report0)
        result = CheckResult(findings_before=report0["findings"])
        self.out(f"Janus check: {self.project.name}: {report0['count']} finding(s) before.")

        redactor = Redactor(secret_values_from_findings(self.project, report0["findings"]))
        self.secret_values = redactor.known
        moved = [(f.get("suggested_env_var") or f["var"], f["file"]) for f in report0["findings"]
                 if f["scanner"] == "secrets" and f.get("suggested_env_var")]
        rules = memory.load_rules(self.project, self.config.rules_path)

        with Workspace(self.project) as ws:
            tests_ok, test_output = self._tests(ws.path)
            result.tests_before = tests_ok
            self.out(f"Tests before: {_status(tests_ok)}")
            findings = report0["findings"]
            notes = {}
            for round_no in range(1, self.config.check.max_rounds + 1):
                if not findings and tests_ok is not False:
                    break
                result.rounds = round_no
                deploy = run_scan(ws.path)["summaries"]["config"]["deploy_configs"]
                strict = sorted({u["var"] for u in find_env_usages(ws.path) if u["mode"] == "strict"})
                tasks, held = subagents.split(findings, ws.path, deploy,
                                              _tail(test_output) if tests_ok is False else None,
                                              strict, notes)
                for rel, first, second in subagents.detect_overlaps(tasks):
                    self.out(f"  note: {rel} is owned by {first}; edits from {second} on it will be refused")
                if not tasks:
                    break
                self.out(f"Round {round_no}: {len(findings)} finding(s), tests {_status(tests_ok)}; "
                         f"tasks in parallel: {', '.join(t.name for t in tasks)}"
                         + (f" ({len(held)} held back)" if held else ""))
                replies = self._run_tasks(tasks, ws.path, redactor, rules)
                result.task_log.append({
                    "round": round_no,
                    "tasks": {t.name: (str(replies[t.name]) if isinstance(replies[t.name], Exception)
                                       else replies[t.name].get("summary", "")) for t in tasks},
                })
                changes, rejections, notes = self._collect(tasks, replies, ws.path, redactor)
                result.rejections.extend(rejections)
                if not changes:
                    self.out("  no edit accepted in this round; stopping.")
                    break
                ws.write(changes)
                _, findings, tests_ok, test_output = self._verify(ws.path, moved)
                self.out(f"  after round {round_no}: {len(findings)} finding(s), tests {_status(tests_ok)}")

            _, findings, tests_ok, _ = self._verify(ws.path, moved)
            result.findings_after = findings
            result.tests_after = tests_ok
            result.changes = ws.changed_files()
            result.costs = self.costs.as_dict()
            self._finish(ws, result)
        self._save_run(result)
        return result

    def _finish(self, ws: Workspace, result: CheckResult) -> None:
        self.out("")
        self.out(f"Findings: {len(result.findings_before)} before, {len(result.findings_after)} after. "
                 f"Tests: {_status(result.tests_before)} before, {_status(result.tests_after)} after.")
        for f in result.findings_after:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            self.out(f"  left: {f['severity'].upper():<8} {f['detail']}  ({loc})")
        self.out(self.costs.render())
        if not result.changes:
            self.out("No change to write.")
            return
        diff = unified_diff(result.changes)
        shown = diff
        for value in sorted(self.secret_values, key=len, reverse=True):
            shown = shown.replace(value, "<hidden secret>")  # never print a secret, even locally
        self.out("\nProposed changes:\n" + shown)
        regression = result.tests_before is True and result.tests_after is False
        if not result.success:
            self.out("Warning: the scanners or the tests still report problems.")
        if regression:
            self.out("Warning: the tests passed before and fail with these changes.")
            if self.apply:
                self.out("Not written: --apply never writes a test regression. Review the diff instead.")
                return
        if self.apply or self.confirm(diff):
            try:
                ws.write_back(result.changes)
            except StaleProject as exc:
                result.error = str(exc)
                self.out(f"Not written: {exc}")
                return
            result.applied = True
            self.out(f"Written: {len(result.changes)} file(s).")
        else:
            self.out("Dry run: nothing was written. Use --apply to write without asking.")

    def _save_run(self, result: CheckResult) -> None:
        runs = self.project / ".janus" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        data = {
            "project": self.project.name,
            "date": memory.today(),
            "findings_before": result.findings_before,
            "findings_after": result.findings_after,
            "tests_before": result.tests_before,
            "tests_after": result.tests_after,
            "rounds": result.rounds,
            "applied": result.applied,
            "changed_files": sorted(result.changes),
            "tasks": result.task_log,
            "rejections": [{"task": r.task, "op": r.edit.get("op"), "file": r.edit.get("file"),
                            "reason": r.reason} for r in result.rejections],
            "costs": result.costs,
            "learned": False,
        }
        text = json.dumps(data, indent=2)
        (runs / f"{stamp}.json").write_text(text, encoding="utf-8")
        (runs / "last.json").write_text(text, encoding="utf-8")


def _status(ok) -> str:
    return {True: "pass", False: "FAIL", None: "none"}[ok]
