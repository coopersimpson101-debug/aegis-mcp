"""Anthropic Claude provider via the official ``anthropic`` Python SDK (``pip install aegis-mcp[anthropic]``)."""

from __future__ import annotations

import os
from typing import Any

from ..types import ChatRequest, ChatResponse, Message, ProviderError, ToolCall, Usage


def _block_to_dict(b: Any) -> dict[str, Any]:
    if isinstance(b, dict):
        return b
    return b.model_dump(exclude_none=True)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "tool":
            pending_results.append({"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content,
                                    **({"is_error": True} if m.is_error else {})})
            continue
        flush()
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.raw is not None and m.raw_provider == "anthropic":
            # Replay native blocks unchanged (keeps thinking blocks valid for the API).
            out.append({"role": "assistant", "content": [_block_to_dict(b) for b in m.raw]})
        else:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            out.append({"role": "assistant", "content": blocks or m.content})
    flush()
    return out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, api_key_env: str = "ANTHROPIC_API_KEY",
                 client: Any = None, max_retries: int = 2, timeout_s: float = 600.0) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key or os.environ.get(api_key_env),
                                              max_retries=max_retries, timeout=timeout_s)
        self.client = client

    def build_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": req.model, "max_tokens": req.max_tokens,
                              "messages": to_anthropic_messages(req.messages)}
        if req.system:
            kw["system"] = req.system
        if req.tools:
            kw["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                           for t in req.tools]
        effort = req.options.get("effort")
        if effort:
            kw["output_config"] = {"effort": effort}
        return kw

    async def complete(self, req: ChatRequest) -> ChatResponse:
        kw = self.build_kwargs(req)
        try:
            import anthropic

            api_errors: tuple[type[BaseException], ...] = (anthropic.APIStatusError, anthropic.APIConnectionError)
        except ImportError:  # pragma: no cover - injected fake client without SDK installed
            api_errors = ()
        try:
            msg = await self.client.messages.create(**kw)
        except api_errors as exc:  # rate limits, overload, auth, network: let the router pick another tier
            status = getattr(exc, "status_code", None)
            raise ProviderError(f"anthropic {status or 'connection'} error: {exc}", retryable=True) from exc
        if msg.stop_reason == "refusal":
            raise ProviderError("anthropic: model declined (stop_reason=refusal)", retryable=True)
        text_parts, calls = [], []
        for b in msg.content:
            t = b["type"] if isinstance(b, dict) else b.type
            if t == "text":
                text_parts.append(b["text"] if isinstance(b, dict) else b.text)
            elif t == "tool_use":
                d = _block_to_dict(b)
                calls.append(ToolCall(d["id"], d["name"], dict(d.get("input") or {})))
        return ChatResponse(
            text="".join(text_parts), tool_calls=calls,
            usage=Usage(msg.usage.input_tokens, msg.usage.output_tokens),
            stop_reason=msg.stop_reason, model=getattr(msg, "model", req.model), provider=self.name,
            raw=[_block_to_dict(b) for b in msg.content],
        )
