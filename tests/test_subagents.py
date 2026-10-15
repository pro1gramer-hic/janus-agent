from janus_agent.subagents import detect_overlaps, fix_targets, split


def finding(scanner, kind, file="app/x.py", **extra):
    return {"scanner": scanner, "type": kind, "severity": "high", "file": file, "line": 1, **extra}


def test_findings_are_routed_by_the_file_the_fix_touches(tmp_path):
    findings = [
        finding("config", "missing_in_env_example", var="A"),
        finding("config", "missing_in_deploy_config", var="A", config="deploy/prod.env"),
        finding("secrets", "named_secret", var="TOKEN"),
        finding("docs", "env_var_not_in_code", file="README.md"),
        finding("docs", "undocumented_endpoint", file="app/main.py"),
    ]
    tasks, held = split(findings, tmp_path, ["deploy/prod.env"], None, [])
    by_name = {t.name: t for t in tasks}
    assert set(by_name) == {"config_secrets", "docs"}
    assert {".env.example", "deploy/prod.env", "app/x.py"} <= by_name["config_secrets"].allowed
    assert by_name["docs"].allowed == {"README.md"}
    assert fix_targets(findings[4], tmp_path) == ["README.md"]
    assert not held and not detect_overlaps(tasks)


def test_security_claim_waits_for_the_secrets_to_move(tmp_path):
    claim = finding("docs", "security_claim_false", file="README.md")
    tasks, held = split([claim, finding("secrets", "named_secret", var="T")], tmp_path, [], None, [])
    assert held == [claim] and all(claim not in t.findings for t in tasks)
    tasks, held = split([claim], tmp_path, [], None, [])
    assert not held and tasks[0].name == "docs"


def test_refusal_notes_alone_do_not_rerun_a_task(tmp_path):
    # Seen with a real small model: a task with no finding left kept running on old notes
    tasks, _ = split([], tmp_path, [], "E KeyError: 'X'", ["X"],
                     notes={"config_secrets": ["An edit was refused"], "tests": ["An edit was refused"]})
    assert [t.name for t in tasks] == ["tests"]


def test_tests_task_only_when_tests_fail(tmp_path):
    tasks, _ = split([], tmp_path, [], None, [])
    assert tasks == []


def test_fail_fast_check_ignores_secrets_not_moved_yet(tmp_path):
    from janus_agent.orchestrator import fail_fast_findings

    (tmp_path / "config.py").write_text('JWT_SECRET = "x"\n')
    assert fail_fast_findings(tmp_path, [("JWT_SECRET", "config.py")], {"JWT_SECRET"}) == []
    (tmp_path / "config.py").write_text('import os\nJWT_SECRET = os.getenv("JWT_SECRET", "dev")\n')
    found = fail_fast_findings(tmp_path, [("JWT_SECRET", "config.py")], set())
    assert found and found[0]["type"] == "secret_not_fail_fast"
