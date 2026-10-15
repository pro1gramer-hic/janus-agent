"""The single interface every model provider implements."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from janus_agent.config import ModelConfig


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str


@dataclass
class ChatRequest:
    system: str
    messages: list
    tool: ToolSpec | None = None  # ask for a native tool call with this schema
    json_mode: bool = False  # ask the server for a JSON object when it supports it
    tag: str = ""  # who is asking (task name), for logs and cost accounting


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None  # only when the server reports it


@dataclass
class ChatResponse:
    text: str = ""
    tool_args: dict | None = None
    usage: Usage = field(default_factory=Usage)


class ProviderError(Exception):
    pass


class ToolsUnsupported(ProviderError):
    """The server rejected the tool definition: fall back to JSON mode."""


class Provider(ABC):
    """Subclass this in a new module of janus_agent.providers to add a provider."""

    name: ClassVar[str]
    supports_tools: ClassVar[bool] = True

    def __init__(self, config: ModelConfig, api_key: str | None = None):
        self.config = config
        self.api_key = api_key

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        ...
