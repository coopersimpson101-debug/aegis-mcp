from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aegis_mcp.safety import Guard, SafetyPolicy


class Notes:
    """Captures confirmation codes the way a human would read them from the server log."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}

    def __call__(self, p: Any) -> None:
        self.codes[p.request_id] = p.code


@pytest.fixture
def notes() -> Notes:
    return Notes()


@pytest.fixture
def make_guard(tmp_path: Path, notes: Notes):
    def _make(server: str = "test", **policy: Any) -> Guard:
        policy.setdefault("audit_log", str(tmp_path / f"{server}-audit.jsonl"))
        policy.setdefault("state_dir", str(tmp_path / "state"))
        return Guard(SafetyPolicy(**policy), server=server, notifier=notes)
    return _make


async def call(client: Any, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an MCP tool and decode the JSON body (raises on tool errors)."""
    r = await client.call_tool(tool, args or {})
    text = "".join(getattr(c, "text", "") for c in r.content)
    if r.is_error:
        raise RuntimeError(text)
    return json.loads(text)
