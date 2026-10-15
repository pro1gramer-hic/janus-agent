"""Get a validated JSON object out of any model.

native mode: the schema is sent as a tool and the model calls it.
json mode:   the schema goes in the prompt and the reply is parsed as JSON.
auto mode:   native first; on the first reply without a real tool call (or a server that
             rejects tools), the whole run switches to json mode.

Every reply is validated. An invalid reply is sent back with the errors, up to
max_attempts calls in total, then StructuredError is raised.
"""

import json
import re
import threading
from collections.abc import Callable

from janus_agent.llm.base import ChatRequest, Message, Provider, ToolSpec, ToolsUnsupported
from janus_agent.llm.schema import validate

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

JSON_INSTRUCTIONS = """
Reply with exactly one JSON object and nothing else: no prose, no code fence.
It must match this JSON schema:
{schema}
"""


class StructuredError(Exception):
    pass


def extract_json(text: str):
    """Return the first JSON object found in text, or None."""
    if not text:
        return None
    candidates = [m.group(1) for m in FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        for start in [i for i, ch in enumerate(chunk) if ch == "{"]:
            try:
                obj, _ = decoder.raw_decode(chunk[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _unwrap_tool_text(obj, tool_name: str):
    """Small models often write the tool call as text: {"name": ..., "arguments": {...}}."""
    if isinstance(obj, dict) and obj.get("name") == tool_name:
        args = obj.get("arguments", obj.get("parameters"))
        if isinstance(args, str):
            args = extract_json(args)
        if isinstance(args, dict):
            return args
    return obj


class StructuredCaller:
    def __init__(self, provider: Provider, mode: str = "auto", max_attempts: int = 3,
                 on_usage: Callable | None = None, log: Callable | None = None):
        self.provider = provider
        self.mode = mode
        self.max_attempts = max_attempts
        self.on_usage = on_usage or (lambda tag, usage: None)
        self.log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._native = mode in ("auto", "native") and provider.supports_tools

    @property
    def native(self) -> bool:
        with self._lock:
            return self._native

    def _fall_back_to_json(self, reason: str) -> None:
        with self._lock:
            if self._native and self.mode == "auto":
                self._native = False
                self.log(f"model: switching to strict JSON mode ({reason})")

    def ask(self, system: str, user: str, tool: ToolSpec, tag: str = "",
            check: Callable[[dict], list] | None = None) -> dict:
        """Return a dict that matches tool.schema and passes check (extra semantic rules)."""
        messages = [Message("user", user)]
        errors = ["no attempt made"]
        attempt = 0
        while attempt < self.max_attempts:
            native = self.native
            req_system = system if native else system + JSON_INSTRUCTIONS.format(
                schema=json.dumps(tool.schema, indent=1))
            request = ChatRequest(system=req_system, messages=list(messages),
                                  tool=tool if native else None, json_mode=not native, tag=tag)
            try:
                response = self.provider.complete(request)
            except ToolsUnsupported as exc:
                if self.mode == "native":
                    raise StructuredError(f"the server does not support tools: {exc}") from exc
                self._fall_back_to_json("the server rejected tools")
                continue  # a capability probe, not an attempt
            attempt += 1
            self.on_usage(tag, response.usage)

            if response.tool_args is not None:
                candidate = response.tool_args
            else:
                candidate = _unwrap_tool_text(extract_json(response.text), tool.name)
                if native:
                    self._fall_back_to_json("the model answered without a tool call")

            if candidate is None:
                errors = ["no JSON object found in the reply"]
            else:
                errors = validate(candidate, tool.schema)
                if not errors and check is not None:
                    errors = check(candidate)
            if not errors:
                return candidate

            self.log(f"model [{tag}]: invalid reply (attempt {attempt}/{self.max_attempts}): "
                     f"{errors[0]}")
            previous = response.text or json.dumps(response.tool_args)
            messages += [
                Message("assistant", previous[:4000]),
                Message("user", "Your reply was invalid:\n- " + "\n- ".join(errors[:10])
                        + "\nReply again with only the corrected JSON object."),
            ]
        raise StructuredError(f"no valid reply after {self.max_attempts} attempt(s): "
                              + "; ".join(errors[:3]))
