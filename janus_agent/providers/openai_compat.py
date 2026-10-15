"""Any server that speaks the OpenAI chat completions API.

Covers OpenAI, Ollama (http://localhost:11434/v1), LM Studio (http://localhost:1234/v1),
vLLM, Groq (https://api.groq.com/openai/v1), OpenRouter and self-hosted servers.
"""

import json

from janus_agent.llm.base import ChatRequest, ChatResponse, Provider, ToolsUnsupported, Usage
from janus_agent.llm.http import HTTPStatusError, post_json

# Local Ollama by default: nothing leaves the machine unless base_url says so
DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OpenAICompatProvider(Provider):
    name = "openai_compat"

    def __init__(self, config, api_key=None):
        super().__init__(config, api_key)
        self._response_format_ok = config.extra.get("json_response_format", True)

    def _payload(self, request: ChatRequest) -> dict:
        messages = [{"role": "system", "content": request.system}]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if request.tool is not None:
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": request.tool.name,
                    "description": request.tool.description,
                    "parameters": request.tool.schema,
                },
            }]
            payload["tool_choice"] = {"type": "function", "function": {"name": request.tool.name}}
        elif request.json_mode and self._response_format_ok:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def complete(self, request: ChatRequest) -> ChatResponse:
        url = (self.config.base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = self._payload(request)
        try:
            data = post_json(url, payload, headers, self.config.timeout)
        except HTTPStatusError as exc:
            body = exc.body.lower()
            if exc.status in (400, 404, 422) and request.tool is not None and "tool" in body:
                raise ToolsUnsupported(str(exc)) from exc
            if exc.status in (400, 422) and "response_format" in payload:
                # Some servers do not know response_format: drop it for the rest of the run
                self._response_format_ok = False
                payload.pop("response_format")
                data = post_json(url, payload, headers, self.config.timeout)
            else:
                raise

        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = message.get("content") or ""
        tool_args = None
        for call in message.get("tool_calls") or []:
            raw = call.get("function", {}).get("arguments")
            if isinstance(raw, dict):
                tool_args = raw
            elif isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    text = text or raw  # let the validator report it and retry
                else:
                    tool_args = parsed if isinstance(parsed, dict) else None
            break

        usage = data.get("usage") or {}
        return ChatResponse(
            text=text,
            tool_args=tool_args,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0) or 0,
                output_tokens=usage.get("completion_tokens", 0) or 0,
                cost=usage.get("cost"),  # OpenRouter reports it
            ),
        )
