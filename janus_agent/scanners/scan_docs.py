# Copyright (c) 2026 pro1gramer-hic
# Originally part of Janus (https://github.com/pro1gramer-hic/Janus-bob), MIT License.
# See NOTICE and LICENSE.
"""Scanner 2: claims in the README that the code no longer supports."""

import ast
import difflib
import re
from pathlib import Path

from janus_agent.scanners import scan_secrets
from janus_agent.scanners.scan_config import _const_str, find_env_usages, iter_files

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

ENV_TOKEN = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s`:)]*)")
PORT_CLAIM = re.compile(r"\bport\s+(\d{2,5})\b|(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", re.I)
PYTHON_FILE_CMD = re.compile(r"^\s*(?:\$\s*)?python3?\s+(?!-m\b)([\w./-]+\.py)\b")
APP_CMD = re.compile(r"(?:uvicorn|gunicorn)\s+([\w.]+):(\w+)")
NO_SECRET_CLAIM = re.compile(
    r"no (?:secrets?|credentials?|passwords?|keys?)\b[^.]*\b(?:code|repo|repository|stored)"
    r"|credentials (?:all )?come from (?:the )?environment",
    re.I,
)
PORT_DEFAULT = re.compile(r"""getenv\(\s*["']PORT["']\s*,\s*["']?(\d{2,5})""")
PORT_KWARG = re.compile(r"\bport\s*=\s*(\d{2,5})\b")
APP_ASSIGN = re.compile(r"^(\w+)\s*=\s*(FastAPI|Flask)\(", re.M)


def _norm_route(path: str) -> str:
    path = re.sub(r"\{[^}]*\}|<[^>]*>", "{}", path)
    return path.rstrip("/") or "/"


def _read_tree(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def find_routes(root: Path) -> dict:
    """FastAPI and Flask routes: (METHOD, normalized path) -> location."""
    routes = {}
    for path in iter_files(root, ".py"):
        tree = _read_tree(path)
        if tree is None:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args):
                    continue
                route = _const_str(dec.args[0])
                if not route or not route.startswith("/"):
                    continue
                attr = dec.func.attr.lower()
                if attr in HTTP_METHODS:
                    methods = [attr.upper()]
                elif attr == "route":
                    methods = ["GET"]
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = [m.upper() for m in (_const_str(e) for e in kw.value.elts) if m]
                else:
                    continue
                for method in methods:
                    routes[(method, _norm_route(route))] = {
                        "method": method, "path": route, "file": rel, "line": dec.lineno,
                    }
    return routes


def find_ports_and_app(root: Path):
    ports, app_ref, uses_fastapi = {}, None, False
    for path in iter_files(root, ".py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(root).as_posix()
        for pattern in (PORT_DEFAULT, PORT_KWARG):
            for match in pattern.finditer(text):
                ports.setdefault(int(match.group(1)), rel)
        match = APP_ASSIGN.search(text)
        if match and app_ref is None:
            module = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
            app_ref = (module, match.group(1), match.group(2))
            uses_fastapi = match.group(2) == "FastAPI"
    if not ports and uses_fastapi:
        ports[8000] = "uvicorn default"
    return ports, app_ref


def _run_command(app_ref):
    if not app_ref:
        return None
    module, var, kind = app_ref
    if kind == "FastAPI":
        return f"python -m uvicorn {module}:{var}"
    return f"flask --app {module}:{var} run"


def _finding(kind, severity, line, claim, truth, detail):
    return {
        "scanner": "docs", "type": kind, "severity": severity,
        "file": "README.md", "line": line,
        "claim": claim, "truth": truth, "detail": detail,
    }


def scan(root) -> dict:
    root = Path(root)
    readme = next((root / n for n in ("README.md", "readme.md", "Readme.md") if (root / n).exists()), None)
    if readme is None:
        return {
            "scanner": "docs",
            "summary": {"readme": None, "claims_checked": 0, "false_claims": 0},
            "findings": [_finding("missing_readme", "low", None, None, None, "No README found")],
        }

    env_vars = sorted({u["var"] for u in find_env_usages(root)})
    routes = find_routes(root)
    ports, app_ref = find_ports_and_app(root)
    secrets = scan_secrets.scan(root)["findings"]

    findings, documented, checked = [], set(), 0

    for number, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), start=1):
        # 1. Environment variables named in the README
        for token in ENV_TOKEN.findall(line):
            if "_" not in token and not re.search(r"variable|env", line, re.I):
                continue
            checked += 1
            if token not in env_vars:
                close = difflib.get_close_matches(token, env_vars, n=1, cutoff=0.5)
                truth = close[0] if close else None
                reason = f"the code reads {truth}" if truth else "the code never reads this variable"
                findings.append(_finding("env_var_not_in_code", "medium", number, token, truth,
                                         f"README mentions {token}, but {reason}"))

        # 2. Endpoints
        for method, path in ENDPOINT.findall(line):
            checked += 1
            key = (method, _norm_route(path))
            documented.add(key)
            if key not in routes:
                findings.append(_finding("endpoint_not_in_code", "medium", number, f"{method} {path}", None,
                                         f"README documents {method} {path}, but no such route exists in the code"))

        # 3. Ports
        for match in PORT_CLAIM.finditer(line):
            port = int(match.group(1) or match.group(2))
            checked += 1
            if ports and port not in ports:
                real = ", ".join(str(p) for p in sorted(ports))
                source = ports[min(ports)]
                findings.append(_finding("port_mismatch", "medium", number, str(port), real,
                                         f"README says port {port}, but the app runs on {real} ({source})"))

        # 4. Commands
        match = PYTHON_FILE_CMD.match(line)
        if match:
            checked += 1
            if not (root / match.group(1)).exists():
                cmd = _run_command(app_ref)
                hint = f"start it with: {cmd}" if cmd else "check the real entry point"
                findings.append(_finding("command_file_missing", "medium", number, line.strip(), cmd,
                                         f"README runs {match.group(1)}, which does not exist; {hint}"))
        match = APP_CMD.search(line)
        if match:
            checked += 1
            module_file = root / (match.group(1).replace(".", "/") + ".py")
            text = module_file.read_text(encoding="utf-8", errors="ignore") if module_file.exists() else ""
            if not re.search(rf"^{re.escape(match.group(2))}\s*=", text, re.M):
                cmd = _run_command(app_ref)
                findings.append(_finding("app_reference_broken", "medium", number, line.strip(), cmd,
                                         f"README points to {match.group(1)}:{match.group(2)}, which does not exist"))

        # 5. Security claims, checked against the secrets scanner
        if NO_SECRET_CLAIM.search(line):
            checked += 1
            if secrets:
                where = ", ".join(f"{s['file']}:{s['line']}" for s in secrets[:3])
                findings.append(_finding("security_claim_false", "high", number, line.strip(), where,
                                         f"README claims no secrets are in the code, but "
                                         f"{len(secrets)} hardcoded secret(s) were found ({where})"))

    # Routes that exist in the code but are missing from the README
    for key in sorted(routes):
        if key not in documented:
            route = routes[key]
            findings.append({
                "scanner": "docs", "type": "undocumented_endpoint", "severity": "low",
                "file": route["file"], "line": route["line"],
                "claim": None, "truth": f"{route['method']} {route['path']}",
                "detail": f"{route['method']} {route['path']} exists in the code but is not documented in the README",
            })

    return {
        "scanner": "docs",
        "summary": {
            "readme": readme.name,
            "claims_checked": checked,
            "false_claims": sum(1 for f in findings if f["type"] != "undocumented_endpoint"),
        },
        "findings": findings,
    }
