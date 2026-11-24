from __future__ import annotations

import time

import anyio
import pytest
from mcp import Client

from aegis_mcp.drone import DroneError, build_server
from aegis_mcp.drone.aegislink import AegisLinkBackend, checksum, decode, encode
from aegis_mcp.drone.mavlink import MavlinkBackend
from aegis_mcp.sim.vehicle import FakeAegisLinkFC, FakeMavlinkVehicle

from .conftest import call

HOME = (47.397742, 8.545594)
CFG = {"drone": {"geofence": {"home_lat": HOME[0], "home_lon": HOME[1], "radius_m": 200, "max_alt_m": 40}}}


async def wait_for(client, pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = (await call(client, "drone_telemetry"))["result"]
        if pred(t):
            return t
        await anyio.sleep(0.1)
    raise AssertionError(f"condition not met; last telemetry {t}")


async def confirm(client, tool, args, notes):
    pend = await call(client, tool, args)
    assert pend["status"] == "confirmation_required", pend
    return await call(client, tool, {**args, "confirm_token": notes.codes[pend["request_id"]]})


@pytest.fixture
def vehicle():
    v = FakeMavlinkVehicle(home_lat=HOME[0], home_lon=HOME[1]).start()
    yield v
    v.stop()


@pytest.fixture
def mav(vehicle):
    b = MavlinkBackend(vehicle.connection_string, heartbeat_timeout_s=5, ack_timeout_s=3)
    yield b
    b.close()


async def test_mavlink_full_flight(mav, vehicle, make_guard, notes):
    server = build_server(CFG, backend=mav, guard=make_guard("drone"))
    async with Client(server) as c:
        t = (await call(c, "drone_telemetry"))["result"]
        assert t["connected"] and t["autopilot"] == "MAV_AUTOPILOT_ARDUPILOTMEGA"
        assert t["armed"] is False and t["geofence_ok"] is True and abs(t["lat"] - HOME[0]) < 1e-6
        # arming is blocked until a human confirms
        assert (await call(c, "drone_arm"))["status"] == "confirmation_required"
        assert vehicle.k.armed is False
        assert (await confirm(c, "drone_arm", {}, notes))["status"] == "ok"
        assert vehicle.k.armed is True
        # takeoff above the fence ceiling is refused before reaching the vehicle
        r = await call(c, "drone_takeoff", {"altitude_m": 45})
        assert r["status"] == "blocked" and "exceeds" in r["reason"]
        assert (await confirm(c, "drone_takeoff", {"altitude_m": 10}, notes))["status"] == "ok"
        await wait_for(c, lambda t: t["mode"] == "GUIDED" and t["rel_alt_m"] > 9.5)
        # goto inside the fence works, outside is blocked
        tgt = (HOME[0] + 0.0005, HOME[1])  # ~55 m north
        assert (await call(c, "drone_goto", {"lat": tgt[0], "lon": tgt[1], "altitude_m": 12}))["status"] == "ok"
        await wait_for(c, lambda t: abs(t["lat"] - tgt[0]) < 2e-6)
        far = await call(c, "drone_goto", {"lat": HOME[0] + 0.01, "lon": HOME[1], "altitude_m": 12})
        assert far["status"] == "blocked" and "radius" in far["reason"]
        # non-forced disarm in the air is rejected by the vehicle
        r = await call(c, "drone_disarm")
        assert r["status"] == "blocked" and "DENIED" in r["reason"]
        assert (await call(c, "drone_rtl"))["status"] == "ok"
        t = await wait_for(c, lambda t: not t["armed"], timeout=15)
        assert abs(t["lat"] - HOME[0]) < 1e-5 and t["rel_alt_m"] == 0
    assert "SET_POSITION_TARGET_GLOBAL_INT" in vehicle.received


async def test_mavlink_mode_allowlist_and_land(mav, vehicle, make_guard, notes):
    server = build_server(CFG, backend=mav, guard=make_guard("drone"))
    async with Client(server) as c:
        r = await call(c, "drone_set_mode", {"mode": "AUTO"})
        assert r["status"] == "blocked" and "allowlist" in r["reason"]
        assert (await call(c, "drone_set_mode", {"mode": "loiter"}))["status"] == "ok"
        await wait_for(c, lambda t: t["mode"] == "LOITER")
        assert (await call(c, "drone_land"))["status"] == "ok"
        await wait_for(c, lambda t: t["mode"] == "LAND")
        force = await call(c, "drone_disarm", {"force": True})
        assert force["status"] == "confirmation_required" and force["risk"] == "critical"


async def test_takeoff_rejected_without_arm(mav, vehicle, make_guard, notes):
    server = build_server(CFG, backend=mav, guard=make_guard("drone"))
    async with Client(server) as c:
        r = await confirm(c, "drone_takeoff", {"altitude_m": 5}, notes)
        assert r["status"] == "blocked" and "DENIED" in r["reason"]


async def test_no_geofence_disables_motion(mav, vehicle, make_guard):
    server = build_server({}, backend=mav, guard=make_guard("drone"))
    async with Client(server) as c:
        r = await call(c, "drone_goto", {"lat": HOME[0], "lon": HOME[1], "altitude_m": 5})
        assert r["status"] == "blocked" and "no geofence" in r["reason"]


async def test_dry_run_never_touches_vehicle(mav, vehicle, make_guard):
    server = build_server(CFG, backend=mav, guard=make_guard("drone", dry_run=True))
    async with Client(server) as c:
        r = await call(c, "drone_arm")
        assert r["status"] == "dry_run" and "ARM" in r["would_do"]
    assert vehicle.k.armed is False


def test_mavlink_no_heartbeat_errors():
    b = MavlinkBackend("udpin:127.0.0.1:0", heartbeat_timeout_s=0.5)
    with pytest.raises(DroneError, match="no heartbeat"):
        b.connect()


# --------------------------------------------------------------------------- AEGIS-Link
def test_aegislink_codec_roundtrip():
    frame = encode(7, "GOTO", 47.1234567, 8.5, 12.0)
    assert frame.startswith(b"$AGL,7,GOTO,47.1234567,8.5,12*") and frame.endswith(b"\r\n")
    seq, cmd, args = decode(frame)
    assert (seq, cmd, args) == (7, "GOTO", ["47.1234567", "8.5", "12"])
    # XOR of the bytes between '$' and '*', computed by hand for this fixed frame
    assert checksum("AGL,1,PING") == "6B" and encode(1, "PING") == b"$AGL,1,PING*6B\r\n"
    with pytest.raises(DroneError, match="checksum"):
        decode(b"$AGL,1,PING*00\r\n")
    with pytest.raises(ValueError):
        encode(1, "MODE", "BAD,FIELD")


async def test_aegislink_flight_via_mcp(make_guard, notes):
    fc = FakeAegisLinkFC(home_lat=HOME[0], home_lon=HOME[1])
    backend = AegisLinkBackend(transport=fc, timeout_s=0.5)
    server = build_server(CFG, backend=backend, guard=make_guard("drone"))
    async with Client(server) as c:
        t = (await call(c, "drone_telemetry"))["result"]
        assert t["backend"] == "aegis-link" and t["armed"] is False
        assert (await confirm(c, "drone_arm", {}, notes))["status"] == "ok"
        assert (await confirm(c, "drone_takeoff", {"altitude_m": 8}, notes))["status"] == "ok"
        await wait_for(c, lambda t: t["rel_alt_m"] >= 8)
        assert (await call(c, "drone_goto", {"lat": HOME[0] + 0.0003, "lon": HOME[1], "altitude_m": 8}))["status"] == "ok"
        r = await call(c, "drone_disarm")
        assert r["status"] == "blocked" and "IN_AIR" in r["reason"]
        assert (await call(c, "drone_land"))["status"] == "ok"
        await wait_for(c, lambda t: not t["armed"])
    assert fc.frames[0] == "PING"


def test_aegislink_retries_on_drop_and_corruption():
    fc = FakeAegisLinkFC(drop_first=1, corrupt_first=1)
    b = AegisLinkBackend(transport=fc, timeout_s=0.2, retries=2)
    b.connect()  # PING dropped once, then a corrupted reply, then success
    assert b.firmware == FakeAegisLinkFC.firmware
    assert fc.frames.count("PING") == 3
