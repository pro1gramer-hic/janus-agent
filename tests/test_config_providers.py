import copy
import json

import httpx
import pytest

from janus_agent import providers
from janus_agent.config import ConfigError, ModelConfig, load_config
from janus_agent.llm.base import ChatRequest, Message, ProviderError, ToolSpec, ToolsUnsupported
from janus_agent.providers.anthropic import AnthropicProvider
from janus_agent.providers.openai_compat import OpenAICompatProvider

TOOL = ToolSpec("submit", "d", {"type": "object", "properties": {}})


def test_registry_discovers_every_provider_file():
    names = set(providers.available())
    assert {"openai_compat", "anthropic", "fake"} <= names


def test_unknown_provider():
    with pytest.raises(providers.UnknownProvider):
        providers.create(ModelConfig(provider="nope"))


def test_config_file_env_and_cli_precedence(tmp_path):
    cfg = tmp_path / "janus-agent.toml"
    cfg.write_text('[model]\nprovider = "anthropic"\nmodel = "a"\napi_key_env = "MY_KEY"\n'
                   '[check]\nmax_rounds = 5\n[pricing]\ninput_per_mtok = 1.0\noutput_per_mtok = 2.0\n')
    config = load_config(cfg, {"model": "from-cli"}, environ={"JANUS_BASE_URL": "http://x", "JANUS_MODEL": "env"})
    assert config.model.provider == "anthropic"
    assert config.model.model == "from-cli"
    assert config.model.base_url == "http://x"
    assert config.check.max_rounds == 5 and config.pricing.output_per_mtok == 2.0


def test_api_key_is_refused_in_a_committed_config(tmp_path):
    cfg = tmp_path / "janus-agent.toml"
    cfg.write_text('[model]\napi_key = "abc"\n')
    with pytest.raises(ConfigError, match="api_key is not allowed"):
        load_config(cfg, environ={})


def test_api_key_is_accepted_in_a_local_config(tmp_path):
    cfg = tmp_path / "janus-agent.toml"
    cfg.write_text('[model]\nprovider = "openai_compat"\n')
    (tmp_path / "janus-agent.local.toml").write_text('[model]\napi_key = "abc"\n')
    assert load_config(cfg, environ={}).model.resolve_api_key() == "abc"


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "k")
    assert ModelConfig(api_key_env="SOME_KEY").resolve_api_key() == "k"


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": copy.deepcopy(json), "headers": headers})
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body if isinstance(body, dict) else None,
                              text=body if isinstance(body, str) else None,
                              request=httpx.Request("POST", url))


def test_openai_compat_tool_call(monkeypatch):
    rec = Recorder((200, {"choices": [{"message": {"content": None, "tool_calls": [
        {"function": {"name": "submit", "arguments": json.dumps({"a": 1})}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3}}))
    monkeypatch.setattr("janus_agent.llm.http.httpx.post", rec)
    provider = OpenAICompatProvider(ModelConfig(base_url="http://localhost:11434/v1", model="m"), "key")
    response = provider.complete(ChatRequest("sys", [Message("user", "q")], tool=TOOL))
    assert response.tool_args == {"a": 1}
    assert response.usage.input_tokens == 10 and response.usage.output_tokens == 3
    call = rec.calls[0]
    assert call["url"] == "http://localhost:11434/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer key"
    assert call["json"]["messages"][0] == {"role": "system", "content": "sys"}
    assert call["json"]["tools"][0]["function"]["name"] == "submit"


def test_openai_compat_drops_response_format_when_refused(monkeypatch):
    rec = Recorder((400, "unknown field response_format"),
                   (200, {"choices": [{"message": {"content": "{}"}}]}))
    monkeypatch.setattr("janus_agent.llm.http.httpx.post", rec)
    provider = OpenAICompatProvider(ModelConfig(model="m"))
    assert provider.complete(ChatRequest("s", [Message("user", "q")], json_mode=True)).text == "{}"
    assert "response_format" in rec.calls[0]["json"] and "response_format" not in rec.calls[1]["json"]


def test_openai_compat_tools_unsupported(monkeypatch):
    monkeypatch.setattr("janus_agent.llm.http.httpx.post",
                        Recorder((400, "this model does not support tools")))
    with pytest.raises(ToolsUnsupported):
        OpenAICompatProvider(ModelConfig(model="m")).complete(ChatRequest("s", [Message("user", "q")], tool=TOOL))


def test_anthropic_request_and_parse(monkeypatch):
    rec = Recorder((200, {"content": [{"type": "text", "text": "ok"},
                                      {"type": "tool_use", "name": "submit", "input": {"a": 2}}],
                          "stop_reason": "tool_use",
                          "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "output_tokens": 4}}))
    monkeypatch.setattr("janus_agent.llm.http.httpx.post", rec)
    provider = AnthropicProvider(ModelConfig(provider="anthropic", model="claude-opus-5-5"), "key")
    response = provider.complete(ChatRequest("sys", [Message("user", "q")], tool=TOOL))
    assert response.tool_args == {"a": 2} and response.usage.input_tokens == 10
    call = rec.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "key" and call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["json"]["tool_choice"] == {"type": "auto"}
    assert "temperature" not in call["json"]


def test_anthropic_refusal_is_an_error(monkeypatch):
    monkeypatch.setattr("janus_agent.llm.http.httpx.post",
                        Recorder((200, {"content": [], "stop_reason": "refusal", "usage": {}})))
    with pytest.raises(ProviderError, match="declined"):
        AnthropicProvider(ModelConfig(model="m"), "key").complete(ChatRequest("s", [Message("user", "q")]))


def test_default_addresses_are_per_provider(monkeypatch):
    rec = Recorder((200, {"choices": [{"message": {"content": "{}"}}]}))
    monkeypatch.setattr("janus_agent.llm.http.httpx.post", rec)
    OpenAICompatProvider(ModelConfig()).complete(ChatRequest("s", [Message("user", "q")]))
    assert rec.calls[0]["url"].startswith("http://localhost:11434/v1")  # local by default
