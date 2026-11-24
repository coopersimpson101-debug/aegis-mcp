"""Example configs stay valid, and the operator CLI works."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from mcp import Client

from aegis_core import parse_config
from aegis_core.providers import ScriptedProvider
from aegis_mcp import cli
from aegis_mcp.drone import build_server as build_drone
from aegis_mcp.lab import build_server as build_lab
from aegis_mcp.nas import build_server as build_nas
from aegis_mcp.printer import build_server as build_printer
from aegis_mcp.safety import AuditLog, ConfirmationManager
from aegis_mcp.sim.lab import make_demo_lab

CONFIG = Path(__file__).parents[1] / "config"


def load(name: str, tmp_path: Path) -> dict:
    cfg = tomllib.loads((CONFIG / name).read_text())
    cfg["safety"] = {**cfg.get("safety", {}), "audit_log": str(tmp_path / "a.jsonl"), "state_dir": str(tmp_path / "s")}
    return cfg


@pytest.mark.parametrize("name,builder,kw", [
    ("printer.example.toml", build_printer, {}),
    ("drone.example.toml", build_drone, {}),
    ("nas.example.toml", build_nas, {"smart_runner": lambda a: "{}"}),
    ("lab.example.toml", build_lab, {"mqtt": make_demo_lab()}),
])
async def test_example_server_configs_build(name, builder, kw, tmp_path):
    server = builder(load(name, tmp_path), **kw)
    async with Client(server) as c:
        assert len((await c.list_tools()).tools) >= 4


def test_example_orchestrator_config_parses():
    raw = tomllib.loads((CONFIG / "aegis.example.toml").read_text())
    cfg = parse_config(raw, providers={"anthropic": ScriptedProvider([]), "ollama": ScriptedProvider([])})
    assert [s.name for s in cfg.servers] == ["printer", "nas", "lab"]
    assert cfg.policy.autonomous_max_risk.label == "medium"


def test_cli_pending_approve_and_audit(tmp_path, capsys):
    cm = ConfirmationManager(state_dir=tmp_path, notifier=None)
    p = cm.request("drone_arm", {}, summary="ARM MOTORS", risk="high")
    assert cli.main(["pending", "--state-dir", str(tmp_path)]) == 0
    assert p.request_id in capsys.readouterr().out
    assert cli.main(["approve", p.request_id, "--state-dir", str(tmp_path)]) == 0
    assert cm.consume("drone_arm", {}, p.request_id)
    assert cli.main(["approve", "cr_nope", "--state-dir", str(tmp_path)]) == 1
    log = tmp_path / "audit.jsonl"
    AuditLog(log).record(tool="x", outcome="ok")
    assert cli.main(["audit", str(log)]) == 0
    log.write_text(log.read_text().replace('"ok"', '"OK"'))
    assert cli.main(["audit", str(log)]) == 2
