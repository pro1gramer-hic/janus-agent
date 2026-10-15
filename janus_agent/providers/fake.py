"""A scripted provider for tests and offline demos. No network, no cost.

Replies are given per tag (the task name) as a list consumed in order, or by a handler
function. Every request is recorded, so tests can check what would have left the machine.
From a config file: provider = "fake" and script = "path/to/replies.json", where the file
maps each tag to a list of {"tool_args": {...}} or {"text": "..."} objects.
"""

import json
import threading
from pathlib import Path

from janus_agent.llm.base import ChatRequest, ChatResponse, Provider, ProviderError, Usage


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, config, api_key=None, replies: dict | None = None, handler=None,
                 supports_tools: bool = True):
        super().__init__(config, api_key)
        if replies is None and config is not None and config.extra.get("script"):
            replies = json.loads(Path(config.extra["script"]).read_text(encoding="utf-8"))
        self.replies = {tag: list(items) for tag, items in (replies or {}).items()}
        self.handler = handler
        self.supports_tools = supports_tools
        self.requests = []
        self._lock = threading.Lock()

    def complete(self, request: ChatRequest) -> ChatResponse:
        with self._lock:
            self.requests.append(request)
            if self.handler is not None:
                reply = self.handler(request)
            else:
                queue = self.replies.get(request.tag) or self.replies.get("*")
                if not queue:
                    raise ProviderError(f"fake provider: no scripted reply left for '{request.tag}'")
                reply = queue.pop(0)
        if isinstance(reply, ChatResponse):
            return reply
        if isinstance(reply, str):
            reply = {"text": reply}
        text = reply.get("text", "")
        tool_args = reply.get("tool_args")
        if tool_args is not None and request.tool is None:
            # JSON mode: the scripted object comes back as text
            text, tool_args = json.dumps(tool_args), None
        prompt_chars = len(request.system) + sum(len(m.content) for m in request.messages)
        return ChatResponse(text=text, tool_args=tool_args,
                            usage=Usage(input_tokens=max(1, prompt_chars // 4),
                                        output_tokens=len(text or json.dumps(tool_args)) // 4))

    def all_text_sent(self) -> str:
        """Everything that would have been sent to a real model."""
        parts = []
        for req in self.requests:
            parts.append(req.system)
            parts.extend(m.content for m in req.messages)
        return "\n".join(parts)
