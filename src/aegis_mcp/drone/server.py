"""MCP server for a drone over MAVLink (ArduPilot / PX4 / SITL) or the AEGIS-Link serial protocol."""

from __future__ import annotations

import argparse
import json
from typing import Annotated, Any, Sequence

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import BaseModel, Field

from ..common import cli_main, make_guard, tool_kwargs
from ..safety import Geofence, Guard, Risk, SafetyViolation
from .base import DroneBackend, DroneError


class DroneSettings(BaseModel):
    allowed_modes: list[str] = Field(default_factory=lambda: ["GUIDED", "LOITER", "BRAKE", "ALT_HOLD", "LAND", "RTL"])
    min_battery_pct_to_arm: int = 30
    min_satellites_to_arm: int = 6
    max_takeoff_alt_m: float = 30.0


def make_backend(dcfg: dict[str, Any]) -> DroneBackend:
    kind = dcfg.get("backend", "mavlink").lower()
    if kind == "mavlink":
        from .mavlink import MavlinkBackend

        return MavlinkBackend(dcfg.get("connection", "udpin:127.0.0.1:14550"), baud=int(dcfg.get("baud", 57600)))
    if kind in ("aegis-link", "aegislink", "serial"):
        from .aegislink import AegisLinkBackend

        return AegisLinkBackend(dcfg.get("serial_port"), baud=int(dcfg.get("baud", 115200)))
    raise ValueError(f"unknown drone backend {kind!r}")


def build_server(cfg: dict[str, Any] | None = None, *, backend: DroneBackend | None = None,
                 guard: Guard | None = None) -> MCPServer:
    cfg = cfg or {}
    dcfg = cfg.get("drone", {})
    settings = DroneSettings(**{k: v for k, v in dcfg.items() if k in DroneSettings.model_fields})
    fence = Geofence(**dcfg["geofence"]) if "geofence" in dcfg else None
    backend = backend or make_backend(dcfg)
    guard = guard or make_guard(cfg, "drone")
    allowed_modes = {m.upper() for m in settings.allowed_modes}

    mcp = MCPServer(
        "aegis-drone",
        instructions=(
            "Controls one drone. ALWAYS read drone_telemetry first. Arming and takeoff require human confirmation. "
            "drone_land and drone_rtl are the safe fallbacks; use them if anything looks wrong."
        ),
    )

    async def io(fn: Any, *a: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*a))
        except DroneError as exc:
            raise SafetyViolation(f"vehicle: {exc}") from exc

    def _need_fence() -> Geofence:
        if fence is None:
            raise SafetyViolation("no geofence configured; motion commands are disabled (set [drone.geofence])")
        return fence

    async def telemetry() -> dict[str, Any]:
        t = await io(backend.telemetry)
        if fence is not None and t.get("lat") is not None:
            v = fence.violations(t["lat"], t["lon"], t.get("rel_alt_m") or 0.0)
            t["geofence_ok"] = not v
            if v:
                t["geofence_violations"] = v
        return t

    @mcp.tool(**tool_kwargs(Risk.READ, title="Telemetry", idempotent=True))
    async def drone_telemetry() -> dict:
        """Position, altitude, mode, armed state, battery, GPS and geofence status."""
        return await guard.run("drone_telemetry", {}, telemetry, risk=Risk.READ, summary="read telemetry")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Geofence", idempotent=True, open_world=False))
    async def drone_geofence() -> dict:
        """The configured geofence (home, radius, altitude band, optional polygon)."""
        return await guard.run("drone_geofence", {}, lambda: fence.model_dump() if fence else None,
                               risk=Risk.READ, summary="read geofence")

    @mcp.tool(**tool_kwargs(Risk.HIGH, title="Arm motors"))
    async def drone_arm(confirm_token: str | None = None) -> dict:
        """Arm the motors. Pre-arm checks: battery, satellites, inside geofence. Always needs human confirmation."""
        async def act() -> dict:
            t = await telemetry()
            if t.get("battery_pct") is not None and t["battery_pct"] < settings.min_battery_pct_to_arm:
                raise SafetyViolation(f"battery {t['battery_pct']}% < {settings.min_battery_pct_to_arm}%")
            if t.get("satellites") is not None and t["satellites"] < settings.min_satellites_to_arm:
                raise SafetyViolation(f"only {t['satellites']} satellites")
            if fence is not None and t.get("geofence_ok") is False:
                raise SafetyViolation("vehicle is outside the geofence")
            await io(backend.arm, False)
            return {"armed": True}

        return await guard.run("drone_arm", {}, act, risk=Risk.HIGH, summary="ARM MOTORS", confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.LOW, title="Disarm motors"))
    async def drone_disarm(
        force: Annotated[bool, Field(description="Force-disarm even in flight: the drone WILL FALL. Critical risk.")] = False,
        confirm_token: str | None = None,
    ) -> dict:
        """Disarm motors (vehicle refuses while flying unless force=true, which needs confirmation)."""
        async def act() -> dict:
            await io(backend.disarm, force)
            return {"armed": False}

        risk = Risk.CRITICAL if force else Risk.LOW
        return await guard.run("drone_disarm", {"force": force}, act, risk=risk,
                               summary="FORCE DISARM (vehicle may fall)" if force else "disarm", confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Set flight mode"))
    async def drone_set_mode(mode: Annotated[str, Field(description="e.g. GUIDED, LOITER, LAND, RTL")]) -> dict:
        """Change flight mode. Only modes in the server's allowlist are accepted."""
        m = mode.upper()

        def validate() -> None:
            if m not in allowed_modes:
                raise SafetyViolation(f"mode {m} not in allowlist {sorted(allowed_modes)}")

        async def act() -> dict:
            await io(backend.set_mode, m)
            return {"mode": m}

        risk = Risk.LOW if m in ("LAND", "RTL") else Risk.MEDIUM
        return await guard.run("drone_set_mode", {"mode": m}, act, risk=risk, summary=f"set mode {m}", validate=validate)

    @mcp.tool(**tool_kwargs(Risk.HIGH, title="Take off"))
    async def drone_takeoff(
        altitude_m: Annotated[float, Field(gt=0, le=500, description="Target altitude above home in metres")],
        confirm_token: str | None = None,
    ) -> dict:
        """Take off to an altitude (must already be armed). Checked against geofence ceiling; needs confirmation."""
        def validate() -> None:
            f = _need_fence()
            if altitude_m > min(settings.max_takeoff_alt_m, f.max_alt_m):
                raise SafetyViolation(f"takeoff altitude {altitude_m} m exceeds limit "
                                      f"{min(settings.max_takeoff_alt_m, f.max_alt_m)} m")

        async def act() -> dict:
            await io(backend.takeoff, altitude_m)
            return {"takeoff_to_m": altitude_m}

        return await guard.run("drone_takeoff", {"altitude_m": altitude_m}, act, risk=Risk.HIGH,
                               summary=f"TAKE OFF to {altitude_m} m", validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Go to position"))
    async def drone_goto(
        lat: Annotated[float, Field(ge=-90, le=90)],
        lon: Annotated[float, Field(ge=-180, le=180)],
        altitude_m: Annotated[float, Field(gt=0, le=500, description="Above home, metres")],
        confirm_token: str | None = None,
    ) -> dict:
        """Fly to a coordinate in GUIDED mode. Refused if the target is outside the geofence."""
        args = {"lat": lat, "lon": lon, "altitude_m": altitude_m}

        def validate() -> None:
            _need_fence().check(lat, lon, altitude_m)

        async def act() -> dict:
            await io(backend.goto, lat, lon, altitude_m)
            return {"target": args}

        return await guard.run("drone_goto", args, act, risk=Risk.MEDIUM, summary=f"goto {lat:.6f},{lon:.6f} @ {altitude_m} m",
                               validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.LOW, title="Land"))
    async def drone_land() -> dict:
        """Land at the current position (safe fallback)."""
        async def act() -> dict:
            await io(backend.land)
            return {"landing": True}
        return await guard.run("drone_land", {}, act, risk=Risk.LOW, summary="land")

    @mcp.tool(**tool_kwargs(Risk.LOW, title="Return to launch"))
    async def drone_rtl() -> dict:
        """Return to launch and land (safe fallback)."""
        async def act() -> dict:
            await io(backend.rtl)
            return {"rtl": True}
        return await guard.run("drone_rtl", {}, act, risk=Risk.LOW, summary="return to launch")

    @mcp.resource("drone://telemetry", mime_type="application/json", description="Live telemetry snapshot")
    async def telemetry_resource() -> str:
        try:
            return json.dumps(await telemetry())
        except SafetyViolation as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource("drone://geofence", mime_type="application/json", description="Configured geofence")
    def geofence_resource() -> str:
        return json.dumps(fence.model_dump() if fence else None)

    mcp.aegis_guard = guard  # type: ignore[attr-defined]
    return mcp


def _build_from_cli(cfg: dict[str, Any], args: argparse.Namespace) -> MCPServer:
    d = cfg.setdefault("drone", {})
    if args.connection:
        d["backend"], d["connection"] = "mavlink", args.connection
    if args.serial_port:
        d["backend"], d["serial_port"] = "aegis-link", args.serial_port
    guard = make_guard(cfg, "drone", overrides={"dry_run": True} if args.dry_run else None)
    return build_server(cfg, guard=guard)


def main(argv: Sequence[str] | None = None) -> None:
    def extra(p: argparse.ArgumentParser) -> None:
        p.add_argument("--connection", help="pymavlink connection string, e.g. tcp:127.0.0.1:5760")
        p.add_argument("--serial-port", help="use the AEGIS-Link protocol on this serial port")

    cli_main(_build_from_cli, "aegis-mcp-drone", "MCP server for MAVLink / AEGIS-Link drones", argv, extra)


if __name__ == "__main__":
    main()
