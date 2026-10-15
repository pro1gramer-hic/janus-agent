import pytest

from janus_agent.config import ModelConfig
from janus_agent.llm.base import ChatResponse, ToolSpec, ToolsUnsupported
from janus_agent.llm.structured import StructuredCaller, StructuredError, extract_json
from janus_agent.providers.fake import FakeProvider

TOOL = ToolSpec("submit", "test", {
    "type": "object", "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
})


def fake(**kwargs):
    return FakeProvider(ModelConfig(provider="fake"), **kwargs)


def test_native_tool_call():
    provider = fake(replies={"t": [{"tool_args": {"answer": 42}}]})
    caller = StructuredCaller(provider, "auto")
    assert caller.ask("sys", "q", TOOL, tag="t") == {"answer": 42}
    assert provider.requests[0].tool is TOOL
    assert caller.native


def test_json_mode_puts_schema_in_prompt():
    provider = fake(replies={"t": [{"text": '{"answer": 1}'}]})
    caller = StructuredCaller(provider, "json")
    assert caller.ask("sys", "q", TOOL, tag="t") == {"answer": 1}
    request = provider.requests[0]
    assert request.tool is None and request.json_mode
    assert '"answer"' in request.system


def test_tool_call_written_as_text_is_salvaged_and_switches_to_json():
    # What qwen2.5-coder:3b did on Ollama: the call as text in a code fence
    text = '```json\n{"name": "submit", "arguments": {"answer": 7}}\n```'
    provider = fake(replies={"t": [{"text": text}, {"text": '{"answer": 8}'}]})
    caller = StructuredCaller(provider, "auto")
    assert caller.ask("sys", "q", TOOL, tag="t") == {"answer": 7}
    assert not caller.native
    assert caller.ask("sys", "q", TOOL, tag="t") == {"answer": 8}
    assert provider.requests[1].tool is None


def test_invalid_reply_is_retried_with_the_errors():
    provider = fake(replies={"t": [{"text": "sure!"}, {"text": '{"answer": "x"}'}, {"text": '{"answer": 3}'}]})
    caller = StructuredCaller(provider, "json", max_attempts=3)
    assert caller.ask("sys", "q", TOOL, tag="t") == {"answer": 3}
    last = provider.requests[-1].messages[-1].content
    assert "invalid" in last and "expected integer" in last


def test_gives_up_after_max_attempts():
    provider = fake(replies={"t": [{"text": "no"}] * 5})
    caller = StructuredCaller(provider, "json", max_attempts=3)
    with pytest.raises(StructuredError):
        caller.ask("sys", "q", TOOL, tag="t")
    assert len(provider.requests) == 3


def test_semantic_check_counts_as_invalid():
    provider = fake(replies={"t": [{"tool_args": {"answer": -1}}, {"tool_args": {"answer": 5}}]})
    caller = StructuredCaller(provider, "native")
    result = caller.ask("sys", "q", TOOL, tag="t",
                        check=lambda d: [] if d["answer"] > 0 else ["answer must be positive"])
    assert result == {"answer": 5}


def test_server_without_tools_falls_back_to_json():
    calls = []

    def handler(request):
        calls.append(request.tool)
        if request.tool is not None:
            raise ToolsUnsupported("tools not supported")
        return ChatResponse(text='{"answer": 2}')

    caller = StructuredCaller(fake(handler=handler), "auto")
    assert caller.ask("sys", "q", TOOL) == {"answer": 2}
    assert calls[0] is TOOL and calls[1] is None


def test_usage_is_reported():
    seen = []
    provider = fake(replies={"t": [{"tool_args": {"answer": 1}}]})
    StructuredCaller(provider, on_usage=lambda tag, usage: seen.append((tag, usage))).ask("s", "q", TOOL, tag="t")
    assert seen and seen[0][0] == "t" and seen[0][1].input_tokens > 0


def test_extract_json():
    assert extract_json('blah {"a": 1} blah') == {"a": 1}
    assert extract_json("```json\n{\"a\": [1, 2]}\n```") == {"a": [1, 2]}
    assert extract_json("{broken") is None
