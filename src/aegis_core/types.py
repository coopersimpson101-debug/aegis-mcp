"""Provider-neutral chat types used by the router, providers and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None      # for role == "tool"
    name: str | None = None              # tool name for role == "tool"
    is_error: bool = False
    raw: Any = None                      # provider-native content (e.g. Anthropic blocks) for faithful replay
    raw_provider: str | None = None
    raw_model: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ChatRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 4096
    options: dict[str, Any] = field(default_factory=dict)  # provider-specific (e.g. {"effort": "low"})


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ChatResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str | None
    model: str
    provider: str
    raw: Any = None
    confidence: float | None = None  # optional provider/self-reported confidence in [0, 1]


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, request: ChatRequest) -> ChatResponse: ...
