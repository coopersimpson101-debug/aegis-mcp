"""OpenAI-compatible (``/v1/chat/completions``) and Ollama (``/api/chat``) providers over plain HTTP.

"OpenAI-compatible" covers vLLM, llama.cpp server, LM Studio, LiteLLM, OpenRouter and similar.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from ..types import ChatRequest, ChatResponse, Message, ProviderError, ToolCall, Usage


def _oa_messages(system: str | None, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant":
            d: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                d["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                                   for tc in m.tool_calls]
            out.append(d)
        else:
            out.append({"role": "user", "content": m.content})
    return out


class _HttpProvider:
    name = "http"

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout_s: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s,
                                    transport=transport)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._c.post(path, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}",
                                retryable=r.status_code in (408, 409, 429) or r.status_code >= 500 or r.status_code == 404)
        return r.json()


class OpenAICompatProvider(_HttpProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, *, api_key: str | None = None, api_key_env: str | None = None, **kw: Any) -> None:
        super().__init__(base_url, api_key=api_key or (os.environ.get(api_key_env) if api_key_env else None), **kw)

    async def complete(self, req: ChatRequest) -> ChatResponse:
        body: dict[str, Any] = {"model": req.model, "messages": _oa_messages(req.system, req.messages),
                                "max_tokens": req.max_tokens}
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        body.update(req.options.get("extra_body", {}))
        data = await self._post("/chat/completions", body)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"{self.name}: malformed response {str(data)[:200]}") from exc
        calls = []
        for tc in msg.get("tool_calls") or []:
            args = tc["function"].get("arguments") or "{}"
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                parsed = {"_unparseable_arguments": args}
            calls.append(ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", tc["function"]["name"], parsed))
        u = data.get("usage") or {}
        return ChatResponse(text=msg.get("content") or "", tool_calls=calls,
                            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
                            stop_reason=choice.get("finish_reason"), model=data.get("model", req.model),
                            provider=self.name, raw=msg)


class OllamaProvider(_HttpProvider):
    name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", **kw: Any) -> None:
        super().__init__(base_url, **kw)

    async def complete(self, req: ChatRequest) -> ChatResponse:
        msgs = []
        for m in _oa_messages(req.system, req.messages):
            if m.get("tool_calls"):
                m = {**m, "content": m["content"] or "", "tool_calls": [
                    {"function": {"name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"])}}
                    for tc in m["tool_calls"]]}
            msgs.append(m)
        body: dict[str, Any] = {"model": req.model, "messages": msgs, "stream": False,
                                "options": {"num_predict": req.max_tokens, **req.options.get("ollama_options", {})}}
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        data = await self._post("/api/chat", body)
        msg = data.get("message") or {}
        calls = [ToolCall(f"call_{uuid.uuid4().hex[:8]}", tc["function"]["name"], dict(tc["function"].get("arguments") or {}))
                 for tc in msg.get("tool_calls") or []]
        return ChatResponse(text=msg.get("content") or "", tool_calls=calls,
                            usage=Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0)),
                            stop_reason="length" if data.get("done_reason") == "length" else data.get("done_reason", "stop"),
                            model=data.get("model", req.model), provider=self.name, raw=msg)
