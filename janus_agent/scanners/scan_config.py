# Copyright (c) 2026 pro1gramer-hic
# Originally part of Janus (https://github.com/pro1gramer-hic/Janus-bob), MIT License.
# See NOTICE and LICENSE.
"""Scanner 1: configuration drift between the code and the env files."""

import ast
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "env", "__pycache__", ".git", ".janus", "node_modules", "build", "dist"}


def iter_files(root: Path, suffix: str):
    for path in root.rglob(f"*{suffix}"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _is_os_environ(node) -> bool:
    """os.environ, or a bare `environ` imported with `from os import environ`."""
    if isinstance(node, ast.Name):
        return node.id == "environ"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def find_env_usages(root: Path) -> list:
    """Return every environment variable read in the Python code.

    mode is "strict" (os.environ["X"], crashes if missing),
    "nullable" (os.getenv("X"), returns None if missing)
    or "default" (os.getenv("X", "value")).
    """
    usages = []
    for path in iter_files(root, ".py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            name, mode = None, None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                func = node.func
                is_getenv = func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os"
                is_environ_get = func.attr == "get" and _is_os_environ(func.value)
                if is_getenv or is_environ_get:
                    name = _const_str(node.args[0])
                    has_default = len(node.args) > 1 or any(k.arg == "default" for k in node.keywords)
                    mode = "default" if has_default else "nullable"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getenv" and node.args:
                name = _const_str(node.args[0])
                has_default = len(node.args) > 1 or any(k.arg == "default" for k in node.keywords)
                mode = "default" if has_default else "nullable"
            elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                name = _const_str(node.slice)
                mode = "strict"
            if name:
                usages.append({"var": name, "file": rel, "line": node.lineno, "mode": mode})
    return usages


def read_env_file(path: Path) -> set:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        keys.add(key)
    return keys


def scan(root) -> dict:
    root = Path(root)
    by_var = {}
    for usage in find_env_usages(root):
        by_var.setdefault(usage["var"], []).append(usage)

    example_path = root / ".env.example"
    example_keys = read_env_file(example_path) if example_path.exists() else set()

    # Deployment configs: any *.env file except the real .env, which is never read
    deploy_configs = {
        p.relative_to(root).as_posix(): read_env_file(p)
        for p in iter_files(root, ".env")
        if p.name != ".env"
    }

    findings = []
    for var in sorted(by_var):
        places = by_var[var]
        modes = {p["mode"] for p in places}
        mode = "strict" if "strict" in modes else "nullable" if "nullable" in modes else "default"
        where = {"file": places[0]["file"], "line": places[0]["line"]}

        if var not in example_keys:
            findings.append({
                "scanner": "config",
                "type": "missing_in_env_example",
                "severity": {"strict": "high", "nullable": "medium", "default": "low"}[mode],
                "var": var,
                **where,
                "detail": f"{var} is used in the code but not documented in .env.example",
            })

        if mode == "default":
            continue

        for config_file, keys in deploy_configs.items():
            if var not in keys:
                if mode == "strict":
                    detail = f"{var} is missing from {config_file}: the app will crash when this code runs"
                else:
                    detail = f"{var} is missing from {config_file}: the code will receive None"
                findings.append({
                    "scanner": "config",
                    "type": "missing_in_deploy_config",
                    "severity": "critical" if mode == "strict" else "high",
                    "var": var,
                    "config": config_file,
                    **where,
                    "detail": detail,
                })

    for var in sorted(example_keys - by_var.keys()):
        findings.append({
            "scanner": "config",
            "type": "unused_in_env_example",
            "severity": "low",
            "var": var,
            "file": ".env.example",
            "line": None,
            "detail": f"{var} is documented in .env.example but never read by the code",
        })

    return {
        "scanner": "config",
        "summary": {
            "vars_in_code": len(by_var),
            "documented_in_env_example": len(example_keys),
            "deploy_configs": sorted(deploy_configs),
        },
        "findings": findings,
    }
