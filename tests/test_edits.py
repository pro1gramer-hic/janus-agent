import pytest

from janus_agent.edits import FileState, apply_one, check_shape, unified_diff
from janus_agent.security.redact import Redactor


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text('import os\n\nTOKEN = "abc123secret"\nDEBUG = False\n', encoding="utf-8")
    (tmp_path / ".env.example").write_text("DEBUG=false", encoding="utf-8")
    (tmp_path / ".env").write_text("REAL=value\n", encoding="utf-8")
    return tmp_path


def test_replace_with_placeholder_in_old(project):
    redactor = Redactor()
    shown = redactor.redact('TOKEN = "abc123secret"')
    state = FileState(project)
    apply_one(state, {"op": "replace", "file": "app.py", "old": shown, "new": 'TOKEN = os.environ["TOKEN"]'}, redactor)
    assert 'TOKEN = os.environ["TOKEN"]' in state.current["app.py"]
    assert "abc123secret" not in state.current["app.py"]


def test_replace_tolerates_line_numbers_and_trailing_spaces(project):
    state = FileState(project)
    apply_one(state, {"op": "replace", "file": "app.py", "old": "4 | DEBUG = False   ", "new": "DEBUG = True"},
              Redactor())
    assert "DEBUG = True" in state.current["app.py"]


def test_placeholder_in_new_is_refused(project):
    redactor = Redactor()
    shown = redactor.redact('TOKEN = "abc123secret"')
    with pytest.raises(ValueError, match="placeholder"):
        apply_one(FileState(project), {"op": "replace", "file": "app.py", "old": "DEBUG = False",
                                       "new": shown}, redactor)


def test_old_must_be_unique_and_present(project):
    with pytest.raises(ValueError, match="not found"):
        apply_one(FileState(project), {"op": "replace", "file": "app.py", "old": "nope", "new": "x"}, Redactor())


def test_env_add_never_writes_a_value(project):
    state = FileState(project)
    apply_one(state, {"op": "env_add", "file": ".env.example", "key": "TOKEN", "comment": "x=y"}, Redactor())
    apply_one(state, {"op": "env_add", "file": ".env.example", "key": "DEBUG"}, Redactor())  # already there
    assert state.current[".env.example"] == "DEBUG=false\nTOKEN=  # x y\n"


def test_env_add_default_comment(project):
    state = FileState(project)
    apply_one(state, {"op": "env_add", "file": "deploy/production.env", "key": "TOKEN"}, Redactor())
    assert state.current["deploy/production.env"] == "TOKEN=  # set in the secret manager\n"


def test_env_add_only_on_env_files(project):
    with pytest.raises(ValueError):
        apply_one(FileState(project), {"op": "env_add", "file": "app.py", "key": "X"}, Redactor())


def test_real_env_file_is_never_touched(project):
    with pytest.raises(Exception, match="secret files"):
        apply_one(FileState(project), {"op": "env_add", "file": ".env", "key": "X"}, Redactor())


def test_create_refuses_existing_file(project):
    with pytest.raises(ValueError, match="exists"):
        apply_one(FileState(project), {"op": "create", "file": "app.py", "content": "x"}, Redactor())


def test_check_shape_reports_missing_fields():
    errors = check_shape({"edits": [{"op": "replace", "file": "a"}, {"op": "env_add", "file": "b", "key": "bad key"}]})
    assert len(errors) == 3


def test_unified_diff():
    diff = unified_diff({"a.txt": ("one\n", "two\n"), "new.txt": (None, "hi\n")})
    assert "-one" in diff and "+two" in diff and "/dev/null" in diff
