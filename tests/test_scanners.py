# Copyright (c) 2026 pro1gramer-hic
# Originally part of Janus (https://github.com/pro1gramer-hic/Janus-bob), MIT License.
# See NOTICE and LICENSE.
"""Janus self-tests: the scanners must find every planted issue in example_app."""

from pathlib import Path

from janus_agent.scanners import scan_config, scan_docs, scan_secrets

EXAMPLE = Path(__file__).resolve().parent.parent / "example_app"


def _types(result):
    return {(f["type"], f.get("var")) for f in result["findings"]}


def test_config_scanner_finds_missing_variable():
    found = _types(scan_config.scan(EXAMPLE))
    assert ("missing_in_deploy_config", "NOTIFY_WEBHOOK_URL") in found
    assert ("missing_in_env_example", "NOTIFY_WEBHOOK_URL") in found
    assert ("missing_in_env_example", "PORT") in found


def test_config_scanner_marks_crash_as_critical():
    findings = scan_config.scan(EXAMPLE)["findings"]
    crash = [f for f in findings if f["type"] == "missing_in_deploy_config"]
    assert crash and all(f["severity"] == "critical" for f in crash)


def test_secrets_scanner_finds_hardcoded_secrets_without_printing_them():
    result = scan_secrets.scan(EXAMPLE)
    names = {f["var"] for f in result["findings"]}
    assert names == {"JWT_SECRET", "ADMIN_PASSWORD"}
    for f in result["findings"]:
        assert "admin123" not in f["detail"]
        assert f["suggested_env_var"] == f["var"]


def test_docs_scanner_finds_every_false_claim():
    found = {f["type"] for f in scan_docs.scan(EXAMPLE)["findings"]}
    assert found == {
        "env_var_not_in_code",
        "command_file_missing",
        "port_mismatch",
        "endpoint_not_in_code",
        "security_claim_false",
        "undocumented_endpoint",
    }


def test_docs_scanner_suggests_the_right_truth():
    findings = {f["type"]: f for f in scan_docs.scan(EXAMPLE)["findings"]}
    assert findings["env_var_not_in_code"]["truth"] == "DATABASE_URL"
    assert findings["port_mismatch"]["truth"] == "8000"
    assert findings["command_file_missing"]["truth"] == "python -m uvicorn app.main:app"


def test_clean_project_has_no_findings(tmp_path):
    (tmp_path / "app.py").write_text('import os\nURL = os.getenv("URL", "x")\n', encoding="utf-8")
    (tmp_path / ".env.example").write_text("URL=x\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Clean\n\nSet `URL`.\n", encoding="utf-8")
    assert scan_config.scan(tmp_path)["findings"] == []
    assert scan_secrets.scan(tmp_path)["findings"] == []
    assert scan_docs.scan(tmp_path)["findings"] == []
