"""Anthropic Messages API over plain HTTP.

Recent Claude models reject forced tool choice and sampling parameters, so the tool is
offered with tool_choice "auto" and no temperature is sent. If the model answers in text
instead of calling the tool, the structured caller switches to JSON mode.
"""

from janus_agent.llm.base import ChatRequest, ChatResponse, Provider, ProviderError, Usage
from janus_agent.llm.http import post_json

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if not self.api_key:
            raise ProviderError("no API key: set api_key_env (for example ANTHROPIC_API_KEY)")
        url = (self.config.base_url or DEFAULT_BASE_URL).rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.tool is not None:
            payload["tools"] = [{
                "name": request.tool.name,
                "description": request.tool.description,
                "input_schema": request.tool.schema,
            }]
            payload["tool_choice"] = {"type": "auto"}
            payload["system"] += f"\n\nAnswer by calling the {request.tool.name} tool."
        fallbacks = self.config.extra.get("fallbacks")
        if fallbacks:
            # Server-side fallback when a safety classifier declines the request
            payload["fallbacks"] = fallbacks
            headers["anthropic-beta"] = FALLBACK_BETA

        data = post_json(url, payload, headers, self.config.timeout)
        if data.get("stop_reason") == "refusal":
            raise ProviderError("the model declined the request (stop_reason: refusal)")

        text, tool_args = [], None
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use" and tool_args is None:
                tool_args = block.get("input") if isinstance(block.get("input"), dict) else None
        usage = data.get("usage") or {}
        input_tokens = sum(usage.get(k, 0) or 0 for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return ChatResponse(
            text="".join(text),
            tool_args=tool_args,
            usage=Usage(input_tokens=input_tokens, output_tokens=usage.get("output_tokens", 0) or 0),
        )
