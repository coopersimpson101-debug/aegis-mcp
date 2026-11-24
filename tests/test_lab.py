from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest
from mcp import Client

from aegis_mcp.lab import Device, HomeAssistant, build_server, topic_matches
from aegis_mcp.sim.lab import FakeHomeAssistant, make_demo_lab

from .conftest import call

CFG = tomllib.loads((Path(__file__).parents[1] / "config" / "lab.example.toml").read_text())


@pytest.fixture
def lab(make_guard):
    broker = make_demo_lab()
    fake_ha = FakeHomeAssistant()
    ha = HomeAssistant("http://ha.test", "test-ha-token", transport=fake_ha.transport())
    server = build_server(CFG, mqtt=broker, ha=ha, guard=make_guard("lab"))
    return server, broker, fake_ha


def test_topic_matching():
    assert topic_matches("lab/+/temperature", "lab/env/temperature")
    assert topic_matches("lab/#", "lab/env/temperature")
    assert not topic_matches("lab/+", "lab/env/temperature")
    assert not topic_matches("stat/lamp/POWER", "stat/lamp/POWER/x")


async def test_sensors(lab):
    server, broker, _ = lab
    async with Client(server) as c:
        devs = await call(c, "lab_list_devices")
        assert {d["id"] for d in devs["result"]} >= {"env_temp", "soldering_iron", "co2"}
        t = await call(c, "lab_read_sensor", {"device_id": "env_temp"})
        assert t["result"]["value"] == 22.4 and t["result"]["unit"] == "C"
        h = await call(c, "lab_read_sensor", {"device_id": "env_humidity"})
        assert h["result"]["value"] == 41.0
        co2 = await call(c, "lab_read_sensor", {"device_id": "co2"})
        assert co2["result"]["value"] == 612 and co2["result"]["unit"] == "ppm"
        broker.publish("lab/env/temperature", "23.0")
        allv = await call(c, "lab_read_all_sensors")
        assert {r["device"]: r["value"] for r in allv["result"]}["env_temp"] == 23.0
        bad = await call(c, "lab_read_sensor", {"device_id": "lamp"})
        assert bad["status"] == "blocked"


async def test_relays_and_risk(lab, notes):
    server, broker, fake_ha = lab
    async with Client(server) as c:
        assert (await call(c, "lab_get_state", {"device_id": "lamp"}))["result"]["on"] is True
        off = await call(c, "lab_set_power", {"device_id": "lamp", "on": False})
        assert off["status"] == "ok" and off["result"]["state_confirmed"] and ("cmnd/lamp/POWER", "OFF") in broker.log
        psu = await call(c, "lab_set_power", {"device_id": "bench_psu", "on": True})
        assert psu["status"] == "ok" and psu["risk"] == "medium"
        iron = await call(c, "lab_set_power", {"device_id": "soldering_iron", "on": True})
        assert iron["status"] == "confirmation_required"
        assert ("cmnd/soldering_iron/POWER", "ON") not in broker.log
        iron = await call(c, "lab_set_power", {"device_id": "soldering_iron", "on": True,
                                               "confirm_token": notes.codes[iron["request_id"]]})
        assert iron["status"] == "ok" and iron["result"]["auto_off_in_s"] == 1800
        assert (await call(c, "lab_set_power", {"device_id": "soldering_iron", "on": False}))["risk"] == "low"
        ha = await call(c, "lab_set_power", {"device_id": "fume_extractor", "on": True})
        assert ha["status"] == "ok" and fake_ha.states["switch.fume_extractor"]["state"] == "on"
        unknown = await call(c, "lab_set_power", {"device_id": "toaster", "on": True})
        assert unknown["status"] == "blocked"


async def test_auto_off(make_guard):
    broker = make_demo_lab()
    cfg = {"lab": {"devices": [{"id": "heater", "kind": "outlet", "command_topic": "cmnd/strip1_o1/POWER",
                                "state_topic": "stat/strip1_o1/POWER", "auto_off_s": 0.2}]}}
    server = build_server(cfg, mqtt=broker, guard=make_guard("lab"))
    async with Client(server) as c:
        r = await call(c, "lab_set_power", {"device_id": "heater", "on": True})
        assert r["result"]["state_confirmed"]
        deadline = time.time() + 3
        while time.time() < deadline and broker.latest("stat/strip1_o1/POWER")[0] != "OFF":
            time.sleep(0.05)
        assert broker.latest("stat/strip1_o1/POWER")[0] == "OFF"


async def test_scenes(lab, notes):
    server, broker, fake_ha = lab
    async with Client(server) as c:
        scenes = await call(c, "lab_list_scenes")
        assert set(scenes["result"]) == {"solder", "shutdown"}
        # solder includes the high-risk iron -> whole scene needs confirmation
        pend = await call(c, "lab_activate_scene", {"name": "solder"})
        assert pend["status"] == "confirmation_required" and pend["risk"] == "high"
        ok = await call(c, "lab_activate_scene", {"name": "solder", "confirm_token": notes.codes[pend["request_id"]]})
        assert [a["device"] for a in ok["result"]] == ["fume_extractor", "lamp", "soldering_iron"]
        assert broker.latest("stat/soldering_iron/POWER")[0] == "ON"
        down = await call(c, "lab_activate_scene", {"name": "shutdown"})
        assert down["status"] == "ok" and broker.latest("stat/soldering_iron/POWER")[0] == "OFF"
        assert (await call(c, "lab_activate_scene", {"name": "party"}))["status"] == "blocked"


async def test_home_assistant_allowlists(lab):
    server, _, fake_ha = lab
    async with Client(server) as c:
        s = await call(c, "lab_ha_state", {"entity_id": "light.bench"})
        assert s["result"]["state"] == "off"
        assert (await call(c, "lab_ha_state", {"entity_id": "lock.front_door"}))["status"] == "blocked"
        on = await call(c, "lab_ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.bench",
                                                   "data": {"brightness": 128}})
        assert on["status"] == "ok" and fake_ha.calls[-1] == ("light", "turn_on", {"brightness": 128, "entity_id": "light.bench"})
        r = await call(c, "lab_ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})
        assert r["status"] == "blocked"
        r = await call(c, "lab_ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "switch.fume_extractor"})
        assert r["status"] == "blocked"


def test_device_validation():
    with pytest.raises(ValueError):
        Device(id="x", kind="outlet", transport="ha", entity_id="switch.x", auto_off_s=5)
    with pytest.raises(ValueError):
        build_server({"lab": {"devices": [], "scenes": {"s": {"actions": [{"device": "ghost", "on": True}]}}}},
                     mqtt=make_demo_lab())
