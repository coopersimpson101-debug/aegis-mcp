from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from aegis_core import Orchestrator, OrchestratorPolicy, Router, RouterConfig
from aegis_core.providers import ScriptedProvider, say, use_tool
from aegis_mcp.common import tool_kwargs
from aegis_mcp.nas import build_server as build_nas
from aegis_mcp.printer import OctoPrintBackend
from aegis_mcp.printer import build_server as build_printer
from aegis_mcp.safety import AuditLog, Risk
from aegis_mcp.sim.printers import FakeOctoPrint


def make_router(steps):
    provider = ScriptedProvider(steps)
    cfg = RouterConfig(tiers=[{"name": "only", "provider": "scripted", "model": "claude-haiku-5-5", "max_difficulty": 1.0,
                               "input_usd_per_mtok": 0.10, "output_usd_per_mtok": 0.50}], budget_file=None)
    return Router(cfg, {"scripted": provider}), provider


def third_party_server() -> MCPServer:
    s = MCPServer("legacy")
    calls = []

    @s.tool(**tool_kwargs(Risk.HIGH))
    def wipe_cache(target: str) -> str:
        """A tool from a server that has no confirmation flow of its own."""
        calls.append(target)
        return f"wiped {target}"

    @s.tool()
    def ping() -> str:
        return "pong"

    s.calls = calls  # type: ignore[attr-defined]
    return s


@pytest.fixture
def fleet(tmp_path: Path, make_guard):
    fake = FakeOctoPrint(api_key="k")
    printer = build_printer({}, backend=OctoPrintBackend("http://p.test", api_key="k", transport=fake.transport()),
                            guard=make_guard("printer"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_bytes(b"hello")
    nas = build_nas({"nas": {"roots": {"docs": str(tmp_path / "docs")}, "writable_roots": ["docs"]}},
                    guard=make_guard("nas"), smart_runner=lambda a: "{}")
    legacy = third_party_server()
    return {"printer": printer, "nas": nas, "legacy": legacy}, fake


async def test_aggregates_tools_with_risk(fleet):
    servers, _ = fleet
    router, _ = make_router([])
    async with Orchestrator(router, servers) as orch:
        assert orch.tools["printer__printer_status"].risk == Risk.READ
        assert orch.tools["printer__printer_start_print"].risk == Risk.HIGH
        assert orch.tools["nas__nas_delete"].server_confirms
        assert orch.tools["legacy__ping"].risk == Risk.MEDIUM  # unknown tools are treated as actuating
        specs = {s.name: s for s in orch.tool_specs()}
        assert "risk:high" in specs["printer__printer_start_print"].description


async def test_agent_loop_reads_then_answers(fleet):
    servers, _ = fleet
    router, provider = make_router([use_tool("printer__printer_get_temperatures", call_id="c1"),
                                    say("The bed is at about 21 C and not heating.")])
    audit = AuditLog(None, server="orchestrator")
    async with Orchestrator(router, servers, audit=audit) as orch:
        res = await orch.run("What's the bed temperature?")
    assert res.stopped_reason == "final_answer" and "21" in res.text
    tool_msg = [m for m in res.messages if m.role == "tool"][0]
    assert tool_msg.tool_call_id == "c1" and json.loads(tool_msg.content)["result"]["bed"]["actual_c"] > 0
    assert len(provider.requests) == 2 and provider.requests[0].tools  # tools were offered
    assert res.steps[0].tool_calls[0]["outcome"] == "ok" and res.cost_usd > 0
    assert audit.records[0]["tool"] == "printer__printer_get_temperatures"


async def test_server_confirmation_goes_to_human(fleet, notes):
    servers, fake = fleet
    asked = []

    async def approver(req):
        asked.append(req)
        return notes.codes[req.request_id]  # the human reads the code from the server log

    router, _ = make_router([use_tool("printer__printer_set_temperature", {"heater": "bed", "target_c": 100,
                                                                          "confirm_token": "LLMGUESS"}),
                             say("Bed set to 100 C.")])
    async with Orchestrator(router, servers, approver=approver) as orch:
        res = await orch.run("Heat the bed to 100 for ABS")
    assert asked and asked[0].kind == "server_confirmation" and asked[0].risk == "high"
    assert fake.printer.temps["bed"][1] == 100
    assert res.steps[0].tool_calls[0]["outcome"] == "ok"


async def test_declined_confirmation_blocks(fleet):
    servers, fake = fleet

    async def approver(req):
        return None

    router, _ = make_router([use_tool("printer__printer_start_print", {"filename": "cube.gcode"}), say("ok, not started")])
    async with Orchestrator(router, servers, approver=approver) as orch:
        res = await orch.run("print the cube")
    assert fake.printer.state == "idle"
    assert res.steps[0].tool_calls[0]["outcome"] == "confirmation_declined"


async def test_pre_call_approval_for_tools_without_server_confirmation(fleet):
    servers, _ = fleet
    router, _ = make_router([use_tool("legacy__wipe_cache", {"target": "all"}), say("could not")])
    async with Orchestrator(router, servers) as orch:  # no approver -> high risk denied
        res = await orch.run("wipe it")
    assert servers["legacy"].calls == [] and res.steps[0].tool_calls[0]["outcome"] == "declined_by_human"

    async def yes(req):
        assert req.kind == "pre_call"
        return True

    router, _ = make_router([use_tool("legacy__wipe_cache", {"target": "all"}), say("done")])
    async with Orchestrator(router, servers, approver=yes) as orch:
        await orch.run("wipe it")
    assert servers["legacy"].calls == ["all"]


async def test_deny_list_and_tool_budget(fleet, tmp_path):
    servers, _ = fleet
    policy = OrchestratorPolicy(deny_tools=["nas__nas_delete"], max_tool_calls=1)
    router, provider = make_router([use_tool("nas__nas_delete", {"path": "docs/a.txt"}),
                                    use_tool("nas__nas_list_roots"), say("stopping")])
    async with Orchestrator(router, servers, policy=policy) as orch:
        assert "nas__nas_delete" not in {s.name for s in orch.tool_specs()}
        res = await orch.run("delete a.txt then list roots")
    outcomes = [c["outcome"] for s in res.steps for c in s.tool_calls]
    assert outcomes == ["denied_by_policy", "budget_exhausted"]
    assert (tmp_path / "docs" / "a.txt").exists()


async def test_plan_first_and_max_steps(fleet):
    servers, _ = fleet
    router, provider = make_router([say("1. read status 2. report")] + [use_tool("printer__printer_status")] * 3)
    async with Orchestrator(router, servers, policy=OrchestratorPolicy(plan_first=True, max_steps=2)) as orch:
        res = await orch.run("status report")
    assert provider.requests[0].tools == [] and provider.requests[1].tools
    assert res.stopped_reason == "max_steps" and len(res.steps) == 3
