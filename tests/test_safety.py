from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis_mcp.safety import (
    AuditLog,
    ConfirmationManager,
    Geofence,
    PathGuard,
    RateLimiter,
    Risk,
    SafetyViolation,
    approve_on_host,
    list_pending_on_host,
    verify_audit_log,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _act():
    return "done"


async def test_guard_confirmation_flow(make_guard, notes):
    g = make_guard()
    r = await g.run("heat", {"t": 1}, _act, risk=Risk.HIGH, summary="heat")
    code = notes.codes[r["request_id"]]
    assert r["status"] == "confirmation_required" and code not in json.dumps(r)  # model never sees the code
    # wrong args -> rejected (token bound to exact arguments)
    assert (await g.run("heat", {"t": 2}, _act, risk=Risk.HIGH, summary="", confirm_token=code))["status"] == "confirmation_required"
    # case-insensitive code accepted once
    assert (await g.run("heat", {"t": 1}, _act, risk=Risk.HIGH, summary="", confirm_token=code.lower()))["status"] == "ok"
    assert (await g.run("heat", {"t": 1}, _act, risk=Risk.HIGH, summary="", confirm_token=code))["status"] == "confirmation_required"


async def test_guard_policy_layers(make_guard):
    g = make_guard(deny_tools=["nuke*"], allow_tools=["safe_*", "nuke_it"], max_risk="high",
                   risk_overrides={"safe_override": "high"}, rate_limits={"safe_rl": [2, 60]})
    assert (await g.run("nuke_it", {}, _act, risk=Risk.READ, summary=""))["status"] == "blocked"   # deny wins
    assert (await g.run("other", {}, _act, risk=Risk.READ, summary=""))["status"] == "blocked"     # not allowlisted
    assert (await g.run("safe_crit", {}, _act, risk=Risk.CRITICAL, summary=""))["status"] == "blocked"  # > max_risk
    assert (await g.run("safe_override", {}, _act, risk=Risk.LOW, summary=""))["status"] == "confirmation_required"
    assert [(await g.run("safe_rl", {}, _act, risk=Risk.READ, summary=""))["status"] for _ in range(3)] == ["ok", "ok", "rate_limited"]


async def test_dry_run_audits_but_does_not_act(make_guard):
    g = make_guard(dry_run=True)
    called = []
    r = await g.run("move", {"x": 1}, lambda: called.append(1), risk=Risk.CRITICAL, summary="move arm")
    assert r == {"status": "dry_run", "tool": "move", "risk": "critical", "would_do": "move arm"} and not called
    assert g.audit.records[-1]["outcome"] == "dry_run"


async def test_validator_and_backend_errors_are_audited(make_guard):
    from mcp.server.mcpserver.exceptions import ToolError

    g = make_guard()

    def bad():
        raise SafetyViolation("geofence: nope")

    r = await g.run("goto", {}, _act, risk=Risk.MEDIUM, summary="", validate=bad)
    assert r["status"] == "blocked" and r["reason"] == "geofence: nope"

    async def boom():
        raise ConnectionError("link lost")

    with pytest.raises(ToolError, match="link lost"):
        await g.run("x", {}, boom, risk=Risk.LOW, summary="")
    assert [rec["outcome"] for rec in g.audit.records] == ["blocked", "error"]


def test_confirmation_expiry_and_host_approval(tmp_path: Path):
    clock = Clock()
    cm = ConfirmationManager(ttl_s=60, state_dir=tmp_path, notifier=None, clock=clock)
    p = cm.request("arm", {}, summary="arm", risk="high")
    clock.t += 61
    assert not cm.consume("arm", {}, p.code)  # expired
    p = cm.request("arm", {}, summary="arm", risk="high")
    # request_id alone is useless until a human approves on the host
    assert not cm.consume("arm", {}, p.request_id)
    assert [x.request_id for x in list_pending_on_host(tmp_path)] == []  # host clock is real time; files use fake clock
    (tmp_path / "pending" / f"{p.request_id}.json").write_text(
        json.dumps({**json.loads((tmp_path / "pending" / f"{p.request_id}.json").read_text()), "expires": 9e12}))
    approve_on_host(tmp_path, p.request_id)
    assert cm.consume("arm", {}, p.request_id)
    assert not (tmp_path / "approved" / f"{p.request_id}.json").exists()  # cleaned up after use
    with pytest.raises(FileNotFoundError):
        approve_on_host(tmp_path, "cr_doesnotexist")


def test_audit_chain_detects_tampering(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, server="t")
    for i in range(3):
        log.record(tool="t", outcome="ok", args={"i": i, "api_key": "SECRET"})
    assert verify_audit_log(path) == (True, 3, "ok")
    text = path.read_text()
    assert "SECRET" not in text and "<redacted>" in text
    # a second writer continues the same chain
    AuditLog(path, server="t").record(tool="t", outcome="ok")
    assert verify_audit_log(path)[0]
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"i":1', '"i":9')
    path.write_text("\n".join(lines) + "\n")
    ok, n, msg = verify_audit_log(path)
    assert not ok and n == 2 and "hash mismatch" in msg
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # deleted a line
    assert "chain broken" in verify_audit_log(path)[2]


def test_rate_limiter_refills():
    clock = Clock()
    rl = RateLimiter({"*": (2, 10.0)}, clock=clock)
    assert [rl.allow("a"), rl.allow("a"), rl.allow("a")] == [True, True, False]
    clock.t += 5  # one token back
    assert rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")  # independent bucket


def test_geofence():
    f = Geofence(home_lat=47.0, home_lon=8.0, radius_m=100, max_alt_m=30)
    assert f.violations(47.0005, 8.0, 10) == []  # ~55 m
    assert "radius" in f.violations(47.002, 8.0, 10)[0]
    assert "altitude" in f.violations(47.0, 8.0, 31)[0]
    poly = Geofence(home_lat=47.0, home_lon=8.0, radius_m=1000, max_alt_m=30,
                    polygon=[(46.999, 7.999), (46.999, 8.001), (47.001, 8.001), (47.001, 7.999)])
    assert poly.violations(47.0, 8.0005, 5) == []
    assert "polygon" in poly.violations(47.0, 8.002, 5)[0]
    with pytest.raises(SafetyViolation):
        poly.check(47.0, 8.002, 5)
    with pytest.raises(ValueError):
        Geofence(home_lat=0, home_lon=0, polygon=[(0, 0), (1, 1)])


def test_path_guard(tmp_path: Path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    pg = PathGuard({"r": tmp_path / "a"})
    assert pg.resolve("r") == (tmp_path / "a").resolve()
    assert pg.resolve("r/b") == (tmp_path / "a" / "b").resolve()
    assert pg.to_virtual(tmp_path / "a" / "b") == "r/b"
    for bad in ["", "r/../x", "/r", "C:\\x", "r/b/../../x", "zz/b", "r/.ssh/id", "r/key.pem"]:
        with pytest.raises(SafetyViolation):
            pg.resolve(bad)
    with pytest.raises(ValueError):
        PathGuard({"bad name": tmp_path})
