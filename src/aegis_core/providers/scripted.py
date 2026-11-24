"""Deterministic provider for tests and offline demos."""

from __future__ import annotations

from typing import Any, Callable

from ..types import ChatRequest, ChatResponse, ProviderError, ToolCall, Usage

Step = ChatResponse | ProviderError | Callable[[ChatRequest], "ChatResponse | ProviderError"]


def say(text: str, *, confidence: float | None = None, stop: str = "end_turn", usage: tuple[int, int] = (100, 20)) -> ChatResponse:
    return ChatResponse(text=text, tool_calls=[], usage=Usage(*usage), stop_reason=stop, model="scripted",
                        provider="scripted", confidence=confidence)


def use_tool(name: str, arguments: dict[str, Any] | None = None, *, call_id: str | None = None,
             text: str = "", usage: tuple[int, int] = (100, 20)) -> ChatResponse:
    return ChatResponse(text=text, tool_calls=[ToolCall(call_id or f"call_{name}", name, arguments or {})],
                        usage=Usage(*usage), stop_reason="tool_use", model="scripted", provider="scripted")


class ScriptedProvider:
    """Plays back a list of steps; records every request it receives."""

    def __init__(self, steps: list[Step], name: str = "scripted") -> None:
        self.steps = list(steps)
        self.name = name
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.steps:
            raise ProviderError(f"{self.name}: script exhausted", retryable=False)
        step = self.steps.pop(0)
        if callable(step) and not isinstance(step, (ChatResponse, ProviderError)):
            step = step(request)
        if isinstance(step, ProviderError):
            raise step
        step.model = request.model
        step.provider = self.name
        return step
