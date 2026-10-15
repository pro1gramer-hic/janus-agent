"""Refuse any edit that weakens or deletes a test, or changes how tests are collected."""

import ast
import re
from pathlib import PurePosixPath

TEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py", ".coveragerc"}
SKIP_PATTERN = re.compile(r"\b(?:pytest\.skip|mark\.skip|skipif|xfail|unittest\.skip|skipTest)\b")
COLLECTION_HOOKS = re.compile(
    r"\b(?:pytest_collection_modifyitems|pytest_ignore_collect|pytest_collect_file|"
    r"pytest_runtest_setup|pytest_runtest_call|pytest_runtest_makereport|collect_ignore\w*|"
    r"pytest_configure|pytest_addoption)\b")


def is_test_file(rel: str) -> bool:
    path = PurePosixPath(rel)
    name = path.name
    return path.suffix == ".py" and (
        name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")
        or "tests" in path.parts or "test" in path.parts)


def _test_functions(tree) -> dict:
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    found[f"{node.name}.{item.name}"] = ast.dump(item)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            found.setdefault(node.name, ast.dump(node))
    return found


def _asserts(tree) -> int:
    return sum(isinstance(node, ast.Assert) for node in ast.walk(tree))


def check(rel: str, before: str | None, after: str) -> list:
    """Return the reasons to refuse this change (empty list: allowed)."""
    name = PurePosixPath(rel).name
    if name in TEST_CONFIG_FILES:
        return [f"{rel}: test configuration is never changed by the agent"]
    if not is_test_file(rel):
        return []
    try:
        new_tree = ast.parse(after)
    except SyntaxError as exc:
        return [f"{rel}: the new test code does not parse ({exc.msg}, line {exc.lineno})"]
    problems = []
    old_text = before or ""
    if len(SKIP_PATTERN.findall(after)) > len(SKIP_PATTERN.findall(old_text)):
        problems.append(f"{rel}: adding skip or xfail to tests is not allowed")
    if len(COLLECTION_HOOKS.findall(after)) > len(COLLECTION_HOOKS.findall(old_text)):
        problems.append(f"{rel}: changing test collection or test hooks is not allowed")
    if before is None:
        return problems
    try:
        old_tree = ast.parse(before)
    except SyntaxError:
        return problems
    old_tests, new_tests = _test_functions(old_tree), _test_functions(new_tree)
    for test, dump in old_tests.items():
        if test not in new_tests:
            problems.append(f"{rel}: test {test} was removed or renamed")
        elif new_tests[test] != dump:
            problems.append(f"{rel}: existing test {test} was modified")
    if _asserts(new_tree) < _asserts(old_tree):
        problems.append(f"{rel}: the number of assertions went down")
    return problems
