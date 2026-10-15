"""Configuration: TOML files, then environment variables, then command line flags.

Files are read from an explicit --config path, or else from the current directory
(janus-agent.toml) and the user config directory (~/.config/janus-agent/config.toml).
The analyzed project is never a config source: a hostile project could otherwise point
base_url at its own server and receive the API key.

API keys never belong in a committed file. The config names the environment variable
that holds the key (api_key_env). A literal api_key is accepted only in a *.local.toml
file, which the .gitignore excludes.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "janus-agent.toml"
LOCAL_CONFIG_NAME = "janus-agent.local.toml"
USER_CONFIG = Path("~/.config/janus-agent/config.toml")

TOOL_CALLING_MODES = ("auto", "native", "json")


class ConfigError(Exception):
    pass


@dataclass
class ModelConfig:
    provider: str = "openai_compat"
    # Default: a local Ollama model, so nothing leaves the machine until configured
    model: str = "qwen2.5-coder:3b"
    base_url: str | None = None  # None: the provider's own default address
    api_key_env: str | None = None
    api_key: str | None = None  # only from a *.local.toml file
    tool_calling: str = "auto"
    max_tokens: int = 4096
    temperature: float | None = 0.0
    timeout: float = 300.0
    # Provider specific settings, passed as is (for example Anthropic "fallbacks")
    extra: dict = field(default_factory=dict)

    def resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or None
        return None


@dataclass
class Pricing:
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


@dataclass
class CheckConfig:
    max_rounds: int = 3
    max_attempts: int = 3
    context_lines: int = 3
    test_command: list = field(default_factory=lambda: ["python", "-m", "pytest", "-q"])
    test_timeout: float = 600.0


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    pricing: Pricing = field(default_factory=Pricing)
    check: CheckConfig = field(default_factory=CheckConfig)
    rules_path: str = ".janus/rules.md"
    sources: list = field(default_factory=list)


def _read_toml(path: Path, local: bool) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from exc
    if not local and "api_key" in data.get("model", {}):
        raise ConfigError(
            f"{path}: api_key is not allowed here. Put the key in an environment variable "
            f"and set api_key_env, or use {LOCAL_CONFIG_NAME} (ignored by git)."
        )
    return data


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _config_files(explicit: Path | None) -> list:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {explicit}")
        files = [explicit]
        local = explicit.with_name(explicit.stem + ".local.toml")
        if local.is_file():
            files.append(local)
        return files
    files = []
    user = USER_CONFIG.expanduser()
    if user.is_file():
        files.append(user)
    for name in (CONFIG_NAME, LOCAL_CONFIG_NAME):
        path = Path.cwd() / name
        if path.is_file():
            files.append(path)
    return files


def load_config(explicit: Path | None = None, overrides: dict | None = None,
                environ: dict | None = None) -> Config:
    environ = os.environ if environ is None else environ
    data, sources = {}, []
    for path in _config_files(explicit):
        data = _merge(data, _read_toml(path, local=path.name.endswith(".local.toml")))
        sources.append(str(path))

    model = dict(data.get("model", {}))
    env_map = {
        "JANUS_PROVIDER": "provider",
        "JANUS_MODEL": "model",
        "JANUS_BASE_URL": "base_url",
        "JANUS_API_KEY_ENV": "api_key_env",
        "JANUS_TOOL_CALLING": "tool_calling",
    }
    for var, key in env_map.items():
        if environ.get(var):
            model[key] = environ[var]
    for key, value in (overrides or {}).items():
        if value is not None:
            model[key] = value

    known = set(ModelConfig.__dataclass_fields__) - {"extra"}
    extra = {k: v for k, v in model.items() if k not in known}
    model_cfg = ModelConfig(**{k: v for k, v in model.items() if k in known}, extra=extra)
    if model_cfg.tool_calling not in TOOL_CALLING_MODES:
        raise ConfigError(f"tool_calling must be one of {TOOL_CALLING_MODES}")

    check = CheckConfig(**{k: v for k, v in data.get("check", {}).items()
                           if k in CheckConfig.__dataclass_fields__})
    if check.max_rounds < 1 or check.max_attempts < 1:
        raise ConfigError("max_rounds and max_attempts must be at least 1")
    pricing = Pricing(**{k: v for k, v in data.get("pricing", {}).items()
                         if k in Pricing.__dataclass_fields__})
    return Config(
        model=model_cfg,
        pricing=pricing,
        check=check,
        rules_path=data.get("memory", {}).get("rules_path", ".janus/rules.md"),
        sources=sources,
    )
