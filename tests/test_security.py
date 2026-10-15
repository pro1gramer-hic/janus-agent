"""Redaction, paths, commands and the test guard.

Strings shaped like provider keys are built at runtime, so no literal in this repository
matches a real key format.
"""

import pytest

from janus_agent.context import finding_view
from janus_agent.security import commands, paths, test_guard
from janus_agent.security.redact import LeakError, Redactor, has_placeholder

FAKE_OPENAI = "sk-" + "proj-" + "Ab1" * 12
FAKE_ANTHROPIC = "sk-" + "ant-" + "Zx9" * 12
FAKE_AWS = "AKIA" + "ABCDEFGHIJ012345"


def test_known_formats_are_redacted_and_restored():
    r = Redactor()
    text = f'client = Client("{FAKE_OPENAI}")\nother = "{FAKE_ANTHROPIC}" # {FAKE_AWS}'
    out = r.redact(text)
    for secret in (FAKE_OPENAI, FAKE_ANTHROPIC, FAKE_AWS):
        assert secret not in out
    assert has_placeholder(out)
    assert r.restore(out) == text


def test_named_secret_values_are_redacted():
    r = Redactor()
    out = r.redact('ADMIN_PASSWORD = "not-a-real-pass"\nJWT_SECRET = os.environ["JWT_SECRET"]\nTIMEOUT = 5')
    assert "not-a-real-pass" not in out
    assert 'os.environ["JWT_SECRET"]' in out
    assert "TIMEOUT = 5" in out


def test_same_value_gets_the_same_placeholder():
    r = Redactor()
    a = r.redact("api_key: abcdef123")
    b = r.redact("token = 'abcdef123'")
    assert a.split(": ")[1] in b


def test_env_files_hide_every_value():
    r = Redactor()
    out = r.redact_env_file("DATABASE_URL=postgresql://u@db:5432/x\nEMPTY=  # set in the secret manager\n# note")
    assert "postgresql" not in out
    assert "EMPTY=  # set in the secret manager" in out and "# note" in out


def test_assert_clean_blocks_known_values_and_formats():
    r = Redactor(["hunter2-secret"])
    with pytest.raises(LeakError):
        r.assert_clean("value is hunter2-secret")
    with pytest.raises(LeakError):
        r.assert_clean(f"key {FAKE_OPENAI}")
    r.assert_clean("nothing to see")


def test_finding_view_drops_masked_value():
    finding = {"scanner": "secrets", "type": "named_secret", "var": "X_TOKEN", "file": "a.py", "line": 1,
               "masked_value": "abcd...(20 chars)", "detail": "X_TOKEN is hardcoded (abcd...(20 chars))"}
    view = finding_view(finding, Redactor())
    assert "masked_value" not in view


@pytest.mark.parametrize("rel", [".env", "app/.env", ".env.production", "deploy/.env.local", "certs/server.pem",
                                 "id_rsa", "janus-agent.local.toml", "../outside.py", "/etc/passwd",
                                 ".git/config", ".janus/report.json"])
def test_forbidden_paths(rel):
    with pytest.raises(paths.PathError):
        paths.check_rel(rel)


@pytest.mark.parametrize("rel", [".env.example", "deploy/production.env", "app/config.py", "README.md"])
def test_allowed_paths(rel):
    assert paths.check_rel(rel) == rel


def test_command_allowlist():
    assert commands.check_command(["python", "-m", "pytest", "-q"])[1:] == ["-m", "pytest", "-q"]
    for bad in (["bash", "-c", "ls"], ["python", "script.py"], ["pytest", "; rm -rf /"], []):
        with pytest.raises(commands.CommandRefused):
            commands.check_command(bad)


def test_test_process_gets_no_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("JWT_SECRET", "x")
    monkeypatch.setenv("JANUS_MODEL", "x")
    monkeypatch.setenv("HARMLESS", "ok")
    env = commands.clean_env()
    assert "OPENAI_API_KEY" not in env and "JWT_SECRET" not in env and "JANUS_MODEL" not in env
    assert env["HARMLESS"] == "ok"


OLD_TEST = "def test_a():\n    assert 1 == 1\n\n\ndef test_b():\n    assert 2 == 2\n"


def test_guard_allows_adding_a_test():
    new = OLD_TEST + "\n\ndef test_c():\n    assert 3 == 3\n"
    assert test_guard.check("tests/test_x.py", OLD_TEST, new) == []


@pytest.mark.parametrize("new", [
    "def test_a():\n    assert 1 == 1\n",  # test removed
    "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 2 == 2\n",  # test modified
    "import pytest\n" + OLD_TEST.replace("def test_b", "@pytest.mark.skip\ndef test_b"),  # skipped
])
def test_guard_refuses_weakening(new):
    assert test_guard.check("tests/test_x.py", OLD_TEST, new)


def test_guard_refuses_collection_hooks_and_test_config():
    hook = "def pytest_collection_modifyitems(items):\n    items.clear()\n"
    assert test_guard.check("tests/conftest.py", None, hook)
    assert test_guard.check("pytest.ini", "[pytest]\n", "[pytest]\naddopts = -k nothing\n")
    assert test_guard.check("tests/conftest.py", None, 'import os\nos.environ.setdefault("A", "test-a")\n') == []
