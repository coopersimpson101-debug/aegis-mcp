"""Pluggable LLM providers. Add your own by implementing ``aegis_core.types.Provider``."""

from __future__ import annotations

from typing import Any

from ..types import Provider
from .anthropic_provider import AnthropicProvider
from .http_providers import OllamaProvider, OpenAICompatProvider
from .scripted import ScriptedProvider, say, use_tool


def make_provider(spec: dict[str, Any]) -> Provider:
    kind = spec.get("type", "").lower()
    opts = {k: v for k, v in spec.items() if k != "type"}
    if kind == "anthropic":
        return AnthropicProvider(**{k: v for k, v in opts.items() if k in ("api_key_env", "max_retries", "timeout_s")})
    if kind in ("openai_compat", "openai"):
        return OpenAICompatProvider(opts.pop("base_url"), **{k: v for k, v in opts.items() if k in ("api_key_env", "timeout_s")})
    if kind == "ollama":
        return OllamaProvider(opts.get("base_url", "http://127.0.0.1:11434"))
    raise ValueError(f"unknown provider type {kind!r}")


__all__ = ["AnthropicProvider", "OllamaProvider", "OpenAICompatProvider", "ScriptedProvider", "make_provider", "say",
           "use_tool"]
