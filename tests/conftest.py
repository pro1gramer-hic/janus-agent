import shutil
from pathlib import Path

import pytest

from janus_agent.config import Config, ModelConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "example_app"


@pytest.fixture
def example_copy(tmp_path) -> Path:
    """A fresh copy of example_app with its 11 planted findings."""
    target = tmp_path / "example_app"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".janus", "__pycache__", ".pytest_cache"))
    return target


@pytest.fixture
def fake_config() -> Config:
    return Config(model=ModelConfig(provider="fake", model="scripted"))
