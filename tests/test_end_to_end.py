"""End to end on example_app with a scripted model: 11 findings to 0, app tests green."""

import datetime
import json
import re
import sys

import pytest

from janus_agent import memory
from janus_agent.learner import LearnError, run_learn
from janus_agent.orchestrator import Checker
from janus_agent.planner import run_plan
from janus_agent.providers.fake import FakeProvider
from janus_agent.scanners import run_scan
from janus_agent.security.redact import secret_values_from_findings

pytest.importorskip("fastapi")

from conftest import EXAMPLE  # noqa: E402

# Read from example_app at run time: the fake secrets live only there
PLANTED_SECRETS = sorted(secret_values_from_findings(EXAMPLE, run_scan(EXAMPLE)["findings"]))


def placeholder(request, name):
    match = re.search(rf'{name} = "(<<REDACTED_\d+>>)"', request.messages[0].content)
    assert match, f"{name} placeholder not shown to the model"
    return match.group(1)


def config_secrets_reply(request):
    jwt, admin = placeholder(request, "JWT_SECRET"), placeholder(request, "ADMIN_PASSWORD")
    edits = [{
        "op": "replace", "file": "app/config.py",
        "old": f'# Temporary, to make login work locally\nJWT_SECRET = "{jwt}"\nADMIN_PASSWORD = "{admin}"',
        "new": 'JWT_SECRET = os.environ["JWT_SECRET"]\nADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]',
    }]
    for key in ("NOTIFY_WEBHOOK_URL", "PORT", "JWT_SECRET", "ADMIN_PASSWORD"):
        edits.append({"op": "env_add", "file": ".env.example", "key": key})
    for key in ("NOTIFY_WEBHOOK_URL", "JWT_SECRET", "ADMIN_PASSWORD"):
        edits.append({"op": "env_add", "file": "deploy/production.env", "key": key})
    return {"summary": "moved secrets, added missing keys", "edits": edits}


DOCS_REPLY = {"summary": "README matches the code", "edits": [
    {"op": "replace", "file": "README.md", "old": "Set the `DB_URL` environment variable",
     "new": "Set the `DATABASE_URL` environment variable"},
    {"op": "replace", "file": "README.md", "old": "python run.py", "new": "python -m uvicorn app.main:app"},
    {"op": "replace", "file": "README.md", "old": "The API serves on port 5000.", "new": "The API serves on port 8000."},
    {"op": "replace", "file": "README.md", "old": "- `GET /stats`: task statistics",
     "new": "- `GET /report/top`: list top tasks"},
]}

TESTS_REPLY = {"summary": "fake env vars for tests", "edits": [{
    "op": "create", "file": "tests/conftest.py",
    "content": 'import os\n\nos.environ.setdefault("JWT_SECRET", "test-jwt")\n'
               'os.environ.setdefault("ADMIN_PASSWORD", "test-admin")\n'
               'os.environ.setdefault("NOTIFY_WEBHOOK_URL", "disabled")\n',
}]}


def scripted_model(request):
    if request.tag == "config_secrets":
        return {"tool_args": config_secrets_reply(request)}
    if request.tag == "docs":
        return {"tool_args": DOCS_REPLY}
    if request.tag == "tests":
        assert "KeyError" in request.messages[0].content  # the real test failure was passed on
        return {"tool_args": TESTS_REPLY}
    raise AssertionError(f"unexpected task {request.tag}")


def run_check(project, config, apply, provider=None):
    provider = provider or FakeProvider(config.model, handler=scripted_model)
    lines = []
    result = Checker(project, config, provider, out=lines.append, apply=apply).run()
    return result, provider, "\n".join(lines)


def test_check_goes_from_11_findings_to_0_with_tests_green(example_copy, fake_config):
    assert run_scan(example_copy)["count"] == 11
    result, provider, output = run_check(example_copy, fake_config, apply=True)

    assert len(result.findings_before) == 11
    assert result.findings_after == [], output
    assert result.tests_before is True and result.tests_after is True, output
    assert result.applied and result.rounds == 2
    assert run_scan(example_copy)["count"] == 0

    config_py = (example_copy / "app/config.py").read_text()
    assert 'os.environ["JWT_SECRET"]' in config_py
    assert not any(secret in config_py for secret in PLANTED_SECRETS)
    assert "NOTIFY_WEBHOOK_URL=  # set in the secret manager" in (example_copy / "deploy/production.env").read_text()
    assert (example_copy / "tests/conftest.py").is_file()
    assert "Model usage:" in output and "config_secrets" in output
    for secret in PLANTED_SECRETS:
        assert secret not in output  # not even in the local diff

    import subprocess
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=example_copy, capture_output=True, text=True)
    assert tests.returncode == 0, tests.stdout


def test_sub_agents_get_only_their_own_findings(example_copy, fake_config):
    _, provider, _ = run_check(example_copy, fake_config, apply=False)
    by_tag = {}
    for request in provider.requests:
        by_tag.setdefault(request.tag, request.messages[0].content)
    assert set(by_tag) == {"config_secrets", "docs", "tests"}
    assert "DB_URL" in by_tag["docs"] and "DB_URL" not in by_tag["config_secrets"]
    assert "JWT_SECRET" in by_tag["config_secrets"]
    assert "security_claim_false" not in by_tag["docs"]  # held back: moving the secrets fixes it


def test_no_secret_ever_reaches_the_model(example_copy, fake_config):
    _, provider, _ = run_check(example_copy, fake_config, apply=False)
    sent = provider.all_text_sent()
    for secret in PLANTED_SECRETS:
        assert secret not in sent
    assert len(PLANTED_SECRETS) == 2
    assert "postgresql://" not in sent  # env file values are hidden too
    assert "masked_value" not in sent


def test_dry_run_writes_nothing(example_copy, fake_config):
    before = {p: p.read_bytes() for p in example_copy.rglob("*") if p.is_file()}
    result, _, output = run_check(example_copy, fake_config, apply=False)
    assert result.changes and not result.applied
    assert "Dry run" in output
    after = {p: p.read_bytes() for p in example_copy.rglob("*") if p.is_file() and ".janus" not in p.parts}
    assert after == before


def test_conflicting_edits_on_the_same_file_are_refused(example_copy, fake_config):
    def model(request):
        if request.tag == "docs":
            # the docs task tries to edit a file owned by config_secrets
            return {"tool_args": {"edits": [
                {"op": "env_add", "file": ".env.example", "key": "SNEAKY"},
                DOCS_REPLY["edits"][0]]}}
        return scripted_model(request)

    result, _, output = run_check(example_copy, fake_config, apply=False,
                                  provider=FakeProvider(fake_config.model, handler=model))
    reasons = [r.reason for r in result.rejections]
    assert any("not in this task's files" in r or "conflict" in r for r in reasons), output
    assert "SNEAKY" not in result.changes.get(".env.example", (None, ""))[1]


def test_weakening_a_test_is_refused(example_copy, fake_config):
    def model(request):
        if request.tag == "tests":
            return {"tool_args": {"edits": [{"op": "replace", "file": "tests/test_main.py",
                                             "old": "assert response.status_code == 200", "new": "assert True"}]}}
        return scripted_model(request)

    fake_config.check.max_rounds = 2
    result, _, _ = run_check(example_copy, fake_config, apply=False,
                             provider=FakeProvider(fake_config.model, handler=model))
    assert any("tests/test_main.py" in r.reason for r in result.rejections)
    assert result.tests_after is False and not result.success


def test_apply_never_writes_a_test_regression(example_copy, fake_config):
    def model(request):
        if request.tag == "tests":
            return {"tool_args": {"edits": []}}  # the conftest fix never comes
        return scripted_model(request)

    original = (example_copy / "app/config.py").read_text()
    result, _, output = run_check(example_copy, fake_config, apply=True,
                                  provider=FakeProvider(fake_config.model, handler=model))
    assert result.tests_before is True and result.tests_after is False
    assert not result.applied and "never writes a test regression" in output
    assert (example_copy / "app/config.py").read_text() == original


def test_each_finding_comes_with_a_suggested_fix(example_copy, fake_config):
    _, provider, _ = run_check(example_copy, fake_config, apply=False)
    config_request = next(r for r in provider.requests if r.tag == "config_secrets")
    assert "Suggested fix: env_add to deploy/production.env with key NOTIFY_WEBHOOK_URL" in config_request.messages[0].content


def test_rounds_are_limited(example_copy, fake_config):
    fake_config.check.max_rounds = 1
    result, _, _ = run_check(example_copy, fake_config, apply=False)
    assert result.rounds == 1
    assert result.tests_after is False  # the conftest fix needed a second round


def test_learn_writes_rules_and_dated_decisions(example_copy, fake_config, monkeypatch):
    run_check(example_copy, fake_config, apply=True)

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2031, 2, 3)

    monkeypatch.setattr(memory.datetime, "date", FixedDate)
    reply = {"rules": [{"title": "Never hardcode secrets", "body": "Read them with os.environ[\"NAME\"]."},
                       {"title": "Keep the README true", "body": "Update it with every code change."}],
             "decisions": [{"index": i, "why": f"reason {i}"} for i in range(11)]}
    provider = FakeProvider(fake_config.model, replies={"learn": [{"tool_args": reply}]})
    assert run_learn(example_copy, fake_config, provider, out=lambda *_: None, apply=True)

    rules = (example_copy / ".janus/rules.md").read_text()
    assert "## Never hardcode secrets" in rules
    decisions = (example_copy / "DECISIONS.md").read_text()
    assert decisions.count("| 2031-02-03 |") == 11
    with pytest.raises(LearnError, match="already learned"):
        run_learn(example_copy, fake_config, provider, out=lambda *_: None, apply=True)


def test_learned_rules_are_sent_with_every_call(example_copy, fake_config):
    rules = example_copy / ".janus" / "rules.md"
    rules.parent.mkdir()
    rules.write_text("## Always say please\nBe polite.\n")
    _, provider, _ = run_check(example_copy, fake_config, apply=False)
    assert all("Always say please" in r.system for r in provider.requests)


def test_learn_refuses_an_unapplied_check(example_copy, fake_config):
    run_check(example_copy, fake_config, apply=False)
    with pytest.raises(LearnError, match="not applied"):
        run_learn(example_copy, fake_config, FakeProvider(fake_config.model), out=lambda *_: None)


def test_plan_writes_a_contract_with_only_real_sites(example_copy, fake_config):
    contract = {
        "summary": "Add a weight to the priority.",
        "files_to_change": [{"file": "app/services.py", "why": "calculate_priority lives here"},
                            {"file": "app/invented.py", "why": "hallucinated"}],
        "call_sites": [{"file": "app/main.py", "line": 19, "why": "computes priority"},
                       {"file": "app/reports.py", "line": 5, "why": "ranks tasks"},
                       {"file": "app/main.py", "line": 999, "why": "hallucinated"}],
        "tests": [{"file": "tests/test_priority.py", "what": "unit tests for calculate_priority"}],
        "config_keys": [],
        "readme_lines": [],
        "risks": ["Changing the score changes the /report/top order."],
    }
    provider = FakeProvider(fake_config.model, replies={"plan": [
        {"tool_args": {"symbols": ["calculate_priority"], "routes": [], "env_vars": [], "keywords": []}},
        {"tool_args": contract}]})
    path = run_plan(example_copy, "give urgent tasks a bigger priority boost", fake_config, provider,
                    out=lambda *_: None)
    text = path.read_text()
    assert "app/services.py" in text and "app/main.py:19" in text and "app/reports.py:5" in text
    assert "invented.py" not in text and ":999" not in text
    assert "tests that reference it: none" in text
    for secret in PLANTED_SECRETS:
        assert secret not in provider.all_text_sent()


def test_run_log_is_saved_for_learn(example_copy, fake_config):
    run_check(example_copy, fake_config, apply=True)
    last = json.loads((example_copy / ".janus/runs/last.json").read_text())
    assert last["applied"] and len(last["findings_before"]) == 11 and last["findings_after"] == []
    assert last["date"] == datetime.date.today().isoformat()
