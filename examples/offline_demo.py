"""End-to-end demo with NO hardware and NO API key.

Spins up the fake OctoPrint and the simulated lab in-process, connects the orchestrator to
both MCP servers, and drives it with a scripted "model". The approver plays the human: it
reads the confirmation code the way you would read it from the server log.

    python examples/offline_demo.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
from pathlib import Path

from aegis_core import Orchestrator, Router, RouterConfig
from aegis_core.providers import ScriptedProvider, say, use_tool
from aegis_mcp.lab import build_server as build_lab
from aegis_mcp.printer import OctoPrintBackend
from aegis_mcp.printer import build_server as build_printer
from aegis_mcp.safety import Guard, SafetyPolicy
from aegis_mcp.sim.lab import make_demo_lab
from aegis_mcp.sim.printers import FakeOctoPrint

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="aegis-demo-"))
    codes: dict[str, str] = {}

    def guard(name: str) -> Guard:
        policy = SafetyPolicy(audit_log=str(tmp / f"{name}.jsonl"), state_dir=str(tmp / "state"))
        return Guard(policy, server=name, notifier=lambda p: codes.__setitem__(p.request_id, p.code))

    octo = FakeOctoPrint(api_key="demo")
    printer = build_printer({}, backend=OctoPrintBackend("http://sim", api_key="demo", transport=octo.transport()),
                            guard=guard("printer"))
    lab_cfg = tomllib.loads((ROOT / "config" / "lab.example.toml").read_text())
    lab_cfg["lab"].pop("homeassistant")
    lab_cfg["lab"]["devices"] = [d for d in lab_cfg["lab"]["devices"] if d.get("transport", "mqtt") == "mqtt"]
    lab_cfg["lab"]["scenes"] = {}
    lab = build_lab(lab_cfg, mqtt=make_demo_lab(), guard=guard("lab"))

    script = ScriptedProvider([
        say("Plan: 1) check the lab temperature 2) check the printer 3) heat the bed for ABS 4) report."),
        use_tool("lab__lab_read_sensor", {"device_id": "env_temp"}, call_id="t1"),
        use_tool("printer__printer_status", call_id="t2"),
        use_tool("printer__printer_set_temperature", {"heater": "bed", "target_c": 100}, call_id="t3"),
        say("Lab is 22.4 C, printer is idle and the bed is now heating to 100 C for ABS."),
    ])
    router = Router(RouterConfig(tiers=[{"name": "scripted", "provider": "scripted", "model": "demo-model",
                                         "max_difficulty": 1.0}], budget_file=None), {"scripted": script})

    async def human(req):
        print(f"\n  [human] asked to confirm {req.tool} ({req.risk}): {req.summary} -> typing code from log")
        return codes.get(req.request_id or "")

    from aegis_core import OrchestratorPolicy

    async with Orchestrator(router, {"printer": printer, "lab": lab}, approver=human,
                            policy=OrchestratorPolicy(plan_first=True)) as orch:
        print(f"connected; {len(orch.tools)} tools: {', '.join(sorted(orch.tools))[:120]}...")
        res = await orch.run("Get the printer ready for an ABS print and tell me the lab conditions.")
    for m in res.messages:
        if m.role == "tool":
            print(f"  tool {m.name}: {json.loads(m.content)['status']}")
    print("\nFINAL:", res.text)
    print("bed target on the fake printer:", octo.printer.temps["bed"][1], "C")
    print("audit logs in", tmp)


if __name__ == "__main__":
    asyncio.run(main())
