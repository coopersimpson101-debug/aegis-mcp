from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from aegis_core import ChatRequest, Message, ProviderError, ToolCall, ToolSpec
from aegis_core.providers import AnthropicProvider, OllamaProvider, OpenAICompatProvider

TOOLS = [ToolSpec("printer__printer_status", "status", {"type": "object", "properties": {}})]
CONVO = [
    Message("user", "bed temp?"),
    Message("assistant", "checking", tool_calls=[ToolCall("t1", "printer__printer_status", {})]),
    Message("tool", '{"status":"ok"}', tool_call_id="t1", name="printer__printer_status"),
]


class FakeMessages:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.kwargs = reply, exc, None

    async def create(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return self.reply


def anthropic_reply(content, stop="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-haiku-5-5",
                           usage=SimpleNamespace(input_tokens=120, output_tokens=30))


async def test_anthropic_request_and_response_mapping():
    msgs = FakeMessages(anthropic_reply([
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Heating."},
        {"type": "tool_use", "id": "t2", "name": "printer__printer_set_temperature", "input": {"heater": "bed", "target_c": 60}},
    ], stop="tool_use"))
    p = AnthropicProvider(client=SimpleNamespace(messages=msgs))
    r = await p.complete(ChatRequest("claude-haiku-5-5", CONVO, system="sys", tools=TOOLS, max_tokens=500,
                                     options={"effort": "low"}))
    kw = msgs.kwargs
    assert kw["model"] == "claude-haiku-5-5" and kw["system"] == "sys" and kw["output_config"] == {"effort": "low"}
    assert "temperature" not in kw and "thinking" not in kw
    assert kw["tools"][0] == {"name": "printer__printer_status", "description": "status", "input_schema": TOOLS[0].input_schema}
    assert kw["messages"][1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "printer__printer_status", "input": {}}
    assert kw["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"status":"ok"}'}]}
    assert r.text == "Heating." and r.tool_calls[0].arguments == {"heater": "bed", "target_c": 60}
    assert r.usage.input_tokens == 120 and r.stop_reason == "tool_use"
    # replaying the assistant turn sends the native blocks (thinking signature intact)
    replay = Message("assistant", r.text, tool_calls=r.tool_calls, raw=r.raw, raw_provider="anthropic")
    await p.complete(ChatRequest("claude-haiku-5-5", [Message("user", "x"), replay,
                                                      Message("tool", "{}", tool_call_id="t2")]))
    assert msgs.kwargs["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}


async def test_anthropic_errors_become_provider_errors():
    anthropic = pytest.importorskip("anthropic")
    exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    p = AnthropicProvider(client=SimpleNamespace(messages=FakeMessages(exc=exc)))
    with pytest.raises(ProviderError) as ei:
        await p.complete(ChatRequest("claude-opus-5-5", [Message("user", "hi")]))
    assert ei.value.retryable
    p2 = AnthropicProvider(client=SimpleNamespace(messages=FakeMessages(anthropic_reply([], stop="refusal"))))
    with pytest.raises(ProviderError, match="refusal"):
        await p2.complete(ChatRequest("claude-opus-5-5", [Message("user", "hi")]))


async def test_openai_compat_mapping():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"model": "qwen", "usage": {"prompt_tokens": 50, "completion_tokens": 7},
                                         "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                                             {"id": "c9", "type": "function", "function": {"name": "nas__nas_list", "arguments": "{\"path\": \"docs\"}"}}]}}]})

    p = OpenAICompatProvider("http://llm.test/v1", api_key="k", transport=httpx.MockTransport(handler))
    r = await p.complete(ChatRequest("qwen", CONVO, system="sys", tools=TOOLS))
    b = seen["body"]
    assert seen["auth"] == "Bearer k" and b["messages"][0] == {"role": "system", "content": "sys"}
    assert b["messages"][2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": '{"status":"ok"}'}
    assert b["tools"][0]["function"]["name"] == "printer__printer_status"
    assert r.tool_calls[0].arguments == {"path": "docs"} and r.usage.output_tokens == 7


async def test_ollama_mapping_and_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["stream"] is False and body["options"]["num_predict"] == 256
        assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == {}
        return httpx.Response(200, json={"model": "llama3.1:8b", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "lab__lab_read_sensor", "arguments": {"device_id": "env_temp"}}}]},
            "prompt_eval_count": 40, "eval_count": 9, "done_reason": "stop"})

    p = OllamaProvider("http://ollama.test", transport=httpx.MockTransport(handler))
    r = await p.complete(ChatRequest("llama3.1:8b", CONVO, tools=TOOLS, max_tokens=256))
    assert r.tool_calls[0].name == "lab__lab_read_sensor" and r.usage.input_tokens == 40
    bad = OllamaProvider("http://ollama.test", transport=httpx.MockTransport(lambda req: httpx.Response(503, text="busy")))
    with pytest.raises(ProviderError) as ei:
        await bad.complete(ChatRequest("m", [Message("user", "x")]))
    assert ei.value.retryable
