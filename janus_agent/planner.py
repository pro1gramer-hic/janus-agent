"""plan: before a change, list what it will touch and write a change contract.

Python builds an index of the code (definitions, references, routes, env vars, README,
tests). The model only picks names from that index and writes the contract from the facts
Python resolved. Any file or line the model cites that is not in the facts is dropped.
"""

import ast
import re
from pathlib import Path

from janus_agent import memory
from janus_agent.config import Config
from janus_agent.costs import CostTracker
from janus_agent.llm.base import ToolSpec
from janus_agent.llm.structured import StructuredCaller, StructuredError
from janus_agent.scanners import run_scan
from janus_agent.scanners.scan_config import find_env_usages, iter_files, read_env_file
from janus_agent.scanners.scan_docs import find_routes
from janus_agent.security import paths
from janus_agent.security.redact import Redactor, secret_values_from_findings
from janus_agent.security.test_guard import is_test_file
from janus_agent.subagents import prompt

EXTRACT_SCHEMA = {
    "type": "object",
    "required": ["symbols", "routes", "env_vars", "keywords"],
    "properties": {
        "symbols": {"type": "array", "items": {"type": "string"}},
        "routes": {"type": "array", "items": {"type": "string"}},
        "env_vars": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
}
ITEM = {"type": "object"}
CONTRACT_SCHEMA = {
    "type": "object",
    "required": ["summary", "files_to_change", "call_sites", "tests", "config_keys", "readme_lines", "risks"],
    "properties": {
        "summary": {"type": "string"},
        "files_to_change": {"type": "array", "items": {
            "type": "object", "required": ["file", "why"],
            "properties": {"file": {"type": "string"}, "why": {"type": "string"}}}},
        "call_sites": {"type": "array", "items": {
            "type": "object", "required": ["file", "line", "why"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "why": {"type": "string"}}}},
        "tests": {"type": "array", "items": {
            "type": "object", "required": ["what"],
            "properties": {"file": {"type": ["string", "null"]}, "what": {"type": "string"}}}},
        "config_keys": {"type": "array", "items": {
            "type": "object", "required": ["key", "where"],
            "properties": {"key": {"type": "string"}, "where": {"type": "string"}}}},
        "readme_lines": {"type": "array", "items": {
            "type": "object", "required": ["line", "change"],
            "properties": {"line": {"type": "integer"}, "change": {"type": "string"}}}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def build_index(root: Path) -> dict:
    definitions, references, test_refs = {}, {}, {}
    for path in iter_files(root, ".py"):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions.setdefault(node.name, []).append((rel, node.lineno))
            name = None
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    references.setdefault(alias.name, set()).add((rel, node.lineno))
            if name:
                references.setdefault(name, set()).add((rel, node.lineno))
                if is_test_file(rel):
                    test_refs.setdefault(name, set()).add(rel)
    readme = next((n for n in ("README.md", "readme.md", "Readme.md") if (root / n).exists()), None)
    readme_lines = (root / readme).read_text(encoding="utf-8").splitlines() if readme else []
    env_files = {p.relative_to(root).as_posix(): read_env_file(p)
                 for p in iter_files(root, ".env") if not paths.is_secret_file(p.name)}
    if (root / ".env.example").is_file():
        env_files[".env.example"] = read_env_file(root / ".env.example")
    return {
        "definitions": definitions,
        "references": references,
        "test_refs": test_refs,
        "routes": find_routes(root),
        "env": find_env_usages(root),
        "env_files": env_files,
        "readme": readme,
        "readme_lines": readme_lines,
    }


def keyword_fallback(description: str, index: dict) -> dict:
    words = {w.lower() for w in WORD.findall(description)}
    symbols = [name for name in index["definitions"]
               if name.lower() in words or any(w in name.lower() for w in words if len(w) >= 5)]
    routes = [m + " " + r["path"] for (m, _), r in index["routes"].items()
              if any(part and part.lower() in words for part in r["path"].strip("/").split("/"))]
    env_vars = sorted({u["var"] for u in index["env"]
                       if u["var"].lower() in words or any(w in u["var"].lower() for w in words if len(w) >= 5)})
    return {"symbols": symbols, "routes": routes, "env_vars": env_vars, "keywords": sorted(words)[:10]}


def _line(root: Path, rel: str, n: int, redactor: Redactor) -> str:
    try:
        return redactor.redact(paths.read_text(root, rel).splitlines()[n - 1].strip())
    except (paths.PathError, OSError, IndexError, UnicodeDecodeError):
        return ""


def resolve_facts(root: Path, picks: dict, index: dict, redactor: Redactor) -> tuple:
    """Return (facts text for the model, allowed sets used to filter the contract)."""
    allowed = {"files": set(), "sites": set(), "readme": set()}
    out = []
    for symbol in picks.get("symbols", []):
        defs = index["definitions"].get(symbol)
        if not defs:
            continue
        out.append(f"Function {symbol}:")
        for rel, n in defs:
            allowed["files"].add(rel)
            out.append(f"  defined at {rel}:{n}: {_line(root, rel, n, redactor)}")
        def_lines = set(defs)
        for rel, n in sorted(index["references"].get(symbol, set()) - def_lines):
            allowed["files"].add(rel)
            allowed["sites"].add((rel, n))
            out.append(f"  used at {rel}:{n}: {_line(root, rel, n, redactor)}")
        tests = sorted(index["test_refs"].get(symbol, set()))
        out.append(f"  tests that reference it: {', '.join(tests) if tests else 'none'}")
    for route in picks.get("routes", []):
        parts = route.split(maxsplit=1)
        if len(parts) != 2:
            continue
        for (method, _), info in index["routes"].items():
            if method == parts[0].upper() and info["path"] == parts[1]:
                allowed["files"].add(info["file"])
                allowed["sites"].add((info["file"], info["line"]))
                out.append(f"Route {route}: {info['file']}:{info['line']}")
                for n, line in enumerate(index["readme_lines"], start=1):
                    if info["path"] in line:
                        allowed["readme"].add(n)
                        out.append(f"  README line {n}: {redactor.redact(line.strip())}")
    for var in picks.get("env_vars", []):
        uses = [u for u in index["env"] if u["var"] == var]
        if not uses:
            continue
        out.append(f"Env var {var}:")
        for u in uses:
            allowed["files"].add(u["file"])
            allowed["sites"].add((u["file"], u["line"]))
            out.append(f"  read at {u['file']}:{u['line']} ({u['mode']})")
        for rel, keys in sorted(index["env_files"].items()):
            allowed["files"].add(rel)
            out.append(f"  in {rel}: {'yes' if var in keys else 'NO'}")
    for word in picks.get("keywords", [])[:10]:
        for n, line in enumerate(index["readme_lines"], start=1):
            if word and word.lower() in line.lower() and n not in allowed["readme"]:
                allowed["readme"].add(n)
                out.append(f"README line {n} mentions '{word}': {redactor.redact(line.strip())}")
    if index["readme"]:
        allowed["files"].add(index["readme"])
    return "\n".join(out) or "No matching code was found.", allowed


def filter_contract(contract: dict, allowed: dict, root: Path) -> dict:
    def file_ok(rel):
        if not rel:
            return False
        try:
            paths.check_rel(rel)
        except paths.PathError:
            return False
        return rel in allowed["files"] or (rel.startswith("tests/") and not (root / rel).exists())

    return {
        "summary": contract["summary"],
        "files_to_change": [f for f in contract["files_to_change"] if file_ok(f["file"])],
        "call_sites": [c for c in contract["call_sites"] if (c["file"], c["line"]) in allowed["sites"]],
        "tests": [t for t in contract["tests"] if t.get("file") is None or file_ok(t["file"])
                  or (root / str(t["file"])).is_file()],
        "config_keys": contract["config_keys"],
        "readme_lines": [r for r in contract["readme_lines"] if r["line"] in allowed["readme"]],
        "risks": contract["risks"],
    }


def render(description: str, contract: dict, facts: str, findings_count: int) -> str:
    def section(title, items):
        return f"## {title}\n\n" + ("\n".join(items) if items else "- none") + "\n"

    return "\n".join([
        f"# Change contract\n\nDate: {memory.today()}\n\nChange: {description}\n",
        f"Summary: {contract['summary']}\n",
        f"Open findings before the change: {findings_count} (run `janus-agent scan`).\n",
        section("Files to change", [f"- `{f['file']}`: {f['why']}" for f in contract["files_to_change"]]),
        section("Call sites affected", [f"- `{c['file']}:{c['line']}`: {c['why']}" for c in contract["call_sites"]]),
        section("Tests to add or update", [f"- {('`' + t['file'] + '`: ') if t.get('file') else ''}{t['what']}"
                                           for t in contract["tests"]]),
        section("Config keys", [f"- `{k['key']}`: {k['where']}" for k in contract["config_keys"]]),
        section("README lines", [f"- line {r['line']}: {r['change']}" for r in contract["readme_lines"]]),
        section("Security risks", [f"- {r}" for r in contract["risks"]]),
        "## Facts found by static analysis\n\n```\n" + facts + "\n```\n",
    ])


def run_plan(project: Path, description: str, config: Config, provider, out=print) -> Path:
    project = Path(project).resolve()
    report = run_scan(project)
    index = build_index(project)
    redactor = Redactor(secret_values_from_findings(project, report["findings"]))
    costs = CostTracker(config.pricing)
    caller = StructuredCaller(provider, config.model.tool_calling, config.check.max_attempts,
                              on_usage=costs.add, log=out)
    rules = memory.load_rules(project, config.rules_path)
    base = prompt("system").split("Edit operations:")[0]
    if rules:
        base += "\nLearned project rules:\n" + rules + "\n"

    route_names = [method + " " + info["path"] for (method, _), info in sorted(index["routes"].items())]
    env_names = sorted({u["var"] for u in index["env"]})
    known = ("Functions and classes: " + ", ".join(sorted(index["definitions"])) + "\n"
             + "Routes: " + ", ".join(route_names) + "\n"
             + "Env vars: " + ", ".join(env_names))
    user = redactor.redact(f"Change: {description}\n\n{known}")
    redactor.assert_clean(base + user)
    try:
        picks = caller.ask(base + "\n" + prompt("plan_extract"), user,
                           ToolSpec("submit_names", "Names the change touches.", EXTRACT_SCHEMA), tag="plan")
    except StructuredError as exc:
        out(f"plan: the model gave no usable answer ({exc}); using keyword matching")
        picks = keyword_fallback(description, index)

    facts, allowed = resolve_facts(project, picks, index, redactor)
    user = redactor.redact(f"Change: {description}\n\nFacts:\n{facts}")
    redactor.assert_clean(user)
    contract = caller.ask(base + "\n" + prompt("plan_contract"), user,
                          ToolSpec("submit_contract", "The change contract.", CONTRACT_SCHEMA), tag="plan")
    contract = filter_contract(contract, allowed, project)

    text = render(description, contract, facts, report["count"])
    path = project / ".janus" / "contract.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    out(text)
    out(costs.render())
    out(f"Contract written to {path.relative_to(project)}. Review it before changing any code.")
    return path
