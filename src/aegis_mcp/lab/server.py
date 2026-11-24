"""MCP server for the lab: MQTT sensors/relays/power strips, Home Assistant entities, scenes."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from fnmatch import fnmatch
from typing import Annotated, Any, Literal, Sequence

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, field_validator, model_validator

from ..common import cli_main, make_guard, tool_kwargs
from ..safety import Guard, Risk, SafetyViolation
from .transports import HomeAssistant, MqttTransport, PahoMqtt


class Device(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: Literal["sensor", "relay", "outlet"]
    transport: Literal["mqtt", "ha"] = "mqtt"
    description: str = ""
    # mqtt
    topic: str | None = None               # sensor value topic
    value_key: str | None = None           # JSON payload key for sensors
    unit: str | None = None
    command_topic: str | None = None
    state_topic: str | None = None
    payload_on: str = "ON"
    payload_off: str = "OFF"
    # home assistant
    entity_id: str | None = None
    # safety
    risk_on: Risk = Risk.MEDIUM
    risk_off: Risk = Risk.LOW
    auto_off_s: float | None = Field(default=None, gt=0, description="Turn off automatically after N seconds on")

    @field_validator("risk_on", "risk_off", mode="before")
    @classmethod
    def _risk(cls, v: Any) -> Risk:
        return Risk.parse(v)

    @model_validator(mode="after")
    def _check(self) -> "Device":
        if self.auto_off_s and self.transport != "mqtt":
            raise ValueError(f"{self.id}: auto_off_s is only supported for MQTT devices")
        if self.transport == "ha" and not self.entity_id:
            raise ValueError(f"{self.id}: Home Assistant devices need entity_id")
        if self.transport == "mqtt" and self.kind == "sensor" and not self.topic:
            raise ValueError(f"{self.id}: MQTT sensors need topic")
        return self


class SceneAction(BaseModel):
    device: str
    on: bool


class Scene(BaseModel):
    description: str = ""
    actions: list[SceneAction]
    risk: Risk | None = None

    @field_validator("risk", mode="before")
    @classmethod
    def _risk(cls, v: Any) -> Risk | None:
        return None if v is None else Risk.parse(v)


class HASettings(BaseModel):
    url: str
    token_env: str = "AEGIS_HA_TOKEN"
    allowed_entities: list[str] = Field(default_factory=lambda: ["sensor.*", "binary_sensor.*", "light.*", "switch.*"])
    allowed_services: list[str] = Field(default_factory=lambda: ["light.turn_on", "light.turn_off", "switch.turn_off"])


def build_server(cfg: dict[str, Any] | None = None, *, mqtt: MqttTransport | None = None,
                 ha: HomeAssistant | None = None, guard: Guard | None = None) -> MCPServer:
    cfg = cfg or {}
    lcfg = cfg.get("lab", {})
    devices = {d.id: d for d in (Device(**x) for x in lcfg.get("devices", []))}
    scenes = {name: Scene(**s) for name, s in lcfg.get("scenes", {}).items()}
    ha_settings = HASettings(**lcfg["homeassistant"]) if "homeassistant" in lcfg else None
    for name, sc in scenes.items():
        for a in sc.actions:
            if a.device not in devices:
                raise ValueError(f"scene {name!r} references unknown device {a.device!r}")
    if mqtt is None and any(d.transport == "mqtt" for d in devices.values()):
        m = lcfg.get("mqtt", {})
        mqtt = PahoMqtt(m.get("host", "127.0.0.1"), int(m.get("port", 1883)), username=m.get("username"),
                        password=os.environ.get(m.get("password_env", "AEGIS_MQTT_PASSWORD")), tls=m.get("tls", False))
    if ha is None and ha_settings is not None:
        ha = HomeAssistant(ha_settings.url, os.environ.get(ha_settings.token_env, ""))
    guard = guard or make_guard(cfg, "lab")
    if mqtt is not None:
        for d in devices.values():
            for t in (d.topic, d.state_topic):
                if d.transport == "mqtt" and t:
                    mqtt.subscribe(t)
    timers: dict[str, threading.Timer] = {}

    mcp = MCPServer("aegis-lab", instructions=(
        "Lab bench control: sensors, relays, power strips and scenes. Use lab_list_devices first. "
        "Some outlets (e.g. heaters, soldering irons) need human confirmation to switch on."))

    def dev(device_id: str, kinds: tuple[str, ...] | None = None) -> Device:
        d = devices.get(device_id)
        if d is None:
            raise SafetyViolation(f"unknown device {device_id!r}; known: {sorted(devices)}")
        if kinds and d.kind not in kinds:
            raise SafetyViolation(f"{device_id} is a {d.kind}, expected {'/'.join(kinds)}")
        return d

    def _need_mqtt() -> MqttTransport:
        if mqtt is None:
            raise SafetyViolation("no MQTT broker configured")
        mqtt.connect()
        return mqtt

    def _need_ha() -> HomeAssistant:
        if ha is None:
            raise SafetyViolation("no Home Assistant configured ([lab.homeassistant])")
        return ha

    async def read_state(d: Device) -> dict[str, Any]:
        if d.transport == "ha":
            s = await _need_ha().get_state(d.entity_id or "")
            if d.kind == "sensor":
                return {"device": d.id, "value": _num(s["state"]), "unit": s["attributes"].get("unit_of_measurement", d.unit),
                        "updated": s.get("last_changed")}
            return {"device": d.id, "on": s["state"] == "on", "raw": s["state"]}
        m = _need_mqtt()
        if d.kind == "sensor":
            got = m.latest(d.topic or "")
            if got is None:
                return {"device": d.id, "value": None, "unit": d.unit, "note": "no message received yet"}
            payload, ts = got
            value: Any = payload
            try:
                parsed = json.loads(payload)
                value = parsed.get(d.value_key or "value") if isinstance(parsed, dict) else parsed
            except json.JSONDecodeError:
                pass
            return {"device": d.id, "value": _num(value), "unit": d.unit, "age_s": round(time.time() - ts, 1)}
        got = m.latest(d.state_topic or "") if d.state_topic else None
        if got is None:
            return {"device": d.id, "on": None, "note": "state unknown (no state topic message)"}
        return {"device": d.id, "on": got[0].strip().upper() == d.payload_on.upper(), "raw": got[0]}

    async def switch(d: Device, on: bool) -> dict[str, Any]:
        if d.transport == "ha":
            domain = (d.entity_id or "").split(".")[0]
            await _need_ha().call_service(domain, "turn_on" if on else "turn_off", {"entity_id": d.entity_id})
        else:
            if not d.command_topic:
                raise SafetyViolation(f"{d.id} has no command_topic")
            m = _need_mqtt()
            m.publish(d.command_topic, d.payload_on if on else d.payload_off)
        confirmed = None
        for _ in range(20):  # wait up to ~2 s for the device to report the new state
            st = await read_state(d)
            if st.get("on") is not None and st["on"] == on:
                confirmed = True
                break
            await anyio.sleep(0.1)
        if on and d.auto_off_s:
            if d.id in timers:
                timers[d.id].cancel()
            def auto_off(d: Device = d) -> None:  # runs on a timer thread; MQTT publish is thread-safe
                assert mqtt is not None and d.command_topic
                mqtt.publish(d.command_topic, d.payload_off)
                guard.audit.record(tool="lab_auto_off", outcome="ok", risk="low", args={"device_id": d.id},
                                   detail=f"auto-off after {d.auto_off_s}s")
                timers.pop(d.id, None)

            t = threading.Timer(d.auto_off_s, auto_off)
            t.daemon = True
            t.start()
            timers[d.id] = t
        elif not on and d.id in timers:
            timers.pop(d.id).cancel()
        return {"device": d.id, "on": on, "state_confirmed": bool(confirmed),
                **({"auto_off_in_s": d.auto_off_s} if on and d.auto_off_s else {})}

    @mcp.tool(**tool_kwargs(Risk.READ, title="List devices", idempotent=True, open_world=False))
    async def lab_list_devices() -> dict:
        """All configured sensors, relays and outlets with their risk when switched on."""
        def fn() -> list[dict[str, Any]]:
            return [{"id": d.id, "kind": d.kind, "transport": d.transport, "description": d.description,
                     "unit": d.unit, "risk_on": d.risk_on.label if d.kind != "sensor" else None,
                     "auto_off_s": d.auto_off_s} for d in devices.values()]
        return await guard.run("lab_list_devices", {}, fn, risk=Risk.READ, summary="list devices")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Read sensor", idempotent=True))
    async def lab_read_sensor(device_id: str) -> dict:
        """Latest value of a sensor (MQTT topic or Home Assistant sensor)."""
        return await guard.run("lab_read_sensor", {"device_id": device_id},
                               lambda: read_state(dev(device_id, ("sensor",))), risk=Risk.READ, summary="read sensor")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Read all sensors", idempotent=True))
    async def lab_read_all_sensors() -> dict:
        """Every sensor value in one call."""
        async def fn() -> list[dict[str, Any]]:
            return [await read_state(d) for d in devices.values() if d.kind == "sensor"]
        return await guard.run("lab_read_all_sensors", {}, fn, risk=Risk.READ, summary="read all sensors")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Get switch state", idempotent=True))
    async def lab_get_state(device_id: str) -> dict:
        """Whether a relay/outlet is on."""
        return await guard.run("lab_get_state", {"device_id": device_id},
                               lambda: read_state(dev(device_id, ("relay", "outlet"))), risk=Risk.READ, summary="read state")

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Switch power"))
    async def lab_set_power(
        device_id: str,
        on: Annotated[bool, Field(description="true = power on, false = power off")],
        confirm_token: str | None = None,
    ) -> dict:
        """Switch a relay or power-strip outlet. Risk is per device (configured); off is usually low risk."""
        d = devices.get(device_id)
        risk = (d.risk_on if on else d.risk_off) if d else Risk.MEDIUM
        return await guard.run("lab_set_power", {"device_id": device_id, "on": on},
                               lambda: switch(dev(device_id, ("relay", "outlet")), on), risk=risk,
                               summary=f"turn {device_id} {'ON' if on else 'OFF'}",
                               validate=lambda: dev(device_id, ("relay", "outlet")) and None, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.READ, title="List scenes", idempotent=True, open_world=False))
    async def lab_list_scenes() -> dict:
        """Configured scenes (named groups of switch actions)."""
        return await guard.run("lab_list_scenes", {}, lambda: {n: s.model_dump(mode="json") for n, s in scenes.items()},
                               risk=Risk.READ, summary="list scenes")

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Activate scene"))
    async def lab_activate_scene(name: str, confirm_token: str | None = None) -> dict:
        """Run every action of a scene in order. Its risk is the highest risk of its actions."""
        sc = scenes.get(name)
        risk = Risk.MEDIUM
        if sc:
            risk = max([sc.risk or Risk.READ] + [devices[a.device].risk_on if a.on else devices[a.device].risk_off
                                                 for a in sc.actions])

        def validate() -> None:
            if sc is None:
                raise SafetyViolation(f"unknown scene {name!r}; known: {sorted(scenes)}")

        async def act() -> list[dict[str, Any]]:
            assert sc is not None
            return [await switch(devices[a.device], a.on) for a in sc.actions]

        return await guard.run("lab_activate_scene", {"name": name}, act, risk=risk, summary=f"activate scene {name}",
                               validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.READ, title="Home Assistant state", idempotent=True))
    async def lab_ha_state(entity_id: str) -> dict:
        """State of an allowlisted Home Assistant entity."""
        def validate() -> None:
            if ha_settings is None or not any(fnmatch(entity_id, p) for p in ha_settings.allowed_entities):
                raise SafetyViolation(f"entity {entity_id!r} not in allowed_entities")
        return await guard.run("lab_ha_state", {"entity_id": entity_id}, lambda: _need_ha().get_state(entity_id),
                               risk=Risk.READ, summary="ha state", validate=validate)

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Home Assistant service"))
    async def lab_ha_call_service(
        domain: Annotated[str, Field(pattern=r"^[a-z_]+$")],
        service: Annotated[str, Field(pattern=r"^[a-z_]+$")],
        entity_id: str,
        data: dict[str, Any] | None = None,
        confirm_token: str | None = None,
    ) -> dict:
        """Call an allowlisted Home Assistant service (e.g. light.turn_on) on an allowlisted entity."""
        def validate() -> None:
            if ha_settings is None:
                raise SafetyViolation("no Home Assistant configured")
            if not any(fnmatch(f"{domain}.{service}", p) for p in ha_settings.allowed_services):
                raise SafetyViolation(f"service {domain}.{service} not in allowed_services")
            if not any(fnmatch(entity_id, p) for p in ha_settings.allowed_entities):
                raise SafetyViolation(f"entity {entity_id!r} not in allowed_entities")
            if not entity_id.startswith(domain + "."):
                raise SafetyViolation("entity domain must match service domain")

        payload = {**(data or {}), "entity_id": entity_id}
        return await guard.run("lab_ha_call_service", {"domain": domain, "service": service, "data": payload},
                               lambda: _need_ha().call_service(domain, service, payload), risk=Risk.MEDIUM,
                               summary=f"{domain}.{service} {entity_id}", validate=validate, confirm_token=confirm_token)

    @mcp.resource("lab://devices", mime_type="application/json", description="Configured lab devices")
    def devices_resource() -> str:
        return json.dumps([d.model_dump(mode="json") for d in devices.values()])

    @mcp.resource("lab://sensors", mime_type="application/json", description="Current sensor values")
    async def sensors_resource() -> str:
        return json.dumps([await read_state(d) for d in devices.values() if d.kind == "sensor"])

    mcp.aegis_guard = guard  # type: ignore[attr-defined]
    return mcp


def _num(v: Any) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() and isinstance(v, str) and "." not in v else f
    except (TypeError, ValueError):
        return v


def _build_from_cli(cfg: dict[str, Any], args: argparse.Namespace) -> MCPServer:
    mqtt = None
    if args.demo:
        from ..sim.lab import make_demo_lab

        mqtt = make_demo_lab()
    guard = make_guard(cfg, "lab", overrides={"dry_run": True} if args.dry_run else None)
    return build_server(cfg, mqtt=mqtt, guard=guard)


def main(argv: Sequence[str] | None = None) -> None:
    def extra(p: argparse.ArgumentParser) -> None:
        p.add_argument("--demo", action="store_true", help="use the in-memory simulated lab instead of a broker")

    cli_main(_build_from_cli, "aegis-mcp-lab", "MCP server for MQTT / Home Assistant lab gear", argv, extra)


if __name__ == "__main__":
    main()
