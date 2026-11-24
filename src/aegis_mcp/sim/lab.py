"""Fake MQTT broker (in-process) with simulated lab devices, and a fake Home Assistant."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from ..lab.transports import CachingMixin, topic_matches
from .http_fake import FakeApp, FakeRequest, Response


class InMemoryBroker(CachingMixin):
    """Implements the lab ``MqttTransport`` protocol without a network.

    ``devices`` are callbacks ``(topic, payload, broker) -> None`` that emulate firmware such
    as Tasmota/ESPHome/Shelly: they react to command topics by publishing new state.
    """

    def __init__(self) -> None:
        self._init_cache()
        self.retained: dict[str, str] = {}
        self.log: list[tuple[str, str]] = []
        self.subscriptions: list[str] = []
        self.devices: list[Callable[[str, str, "InMemoryBroker"], None]] = []
        self._lock = threading.RLock()

    def connect(self) -> None:
        pass

    def subscribe(self, pattern: str) -> None:
        with self._lock:
            self.subscriptions.append(pattern)
            for t, p in self.retained.items():
                if topic_matches(pattern, t):
                    self._store(t, p)

    def publish(self, topic: str, payload: str, *, retain: bool = False, qos: int = 1) -> None:
        with self._lock:
            self.log.append((topic, payload))
            if retain:
                self.retained[topic] = payload
            if any(topic_matches(s, topic) for s in self.subscriptions):
                self._store(topic, payload)
            for dev in list(self.devices):
                dev(topic, payload, self)


def tasmota_plug(name: str, *, initial: bool = False) -> Callable[[str, str, InMemoryBroker], None]:
    """Emulates a Tasmota plug: cmnd/<name>/POWER ON|OFF -> stat/<name>/POWER ON|OFF (retained)."""
    state = {"on": initial}

    def dev(topic: str, payload: str, broker: InMemoryBroker) -> None:
        if topic == f"cmnd/{name}/POWER":
            p = payload.strip().upper()
            state["on"] = (not state["on"]) if p == "TOGGLE" else p in ("ON", "1", "TRUE")
            broker.publish(f"stat/{name}/POWER", "ON" if state["on"] else "OFF", retain=True)
    return dev


def make_demo_lab() -> InMemoryBroker:
    """Broker preloaded with the devices used in examples/lab.example.toml."""
    b = InMemoryBroker()
    b.devices += [tasmota_plug("bench_psu"), tasmota_plug("soldering_iron"), tasmota_plug("lamp", initial=True),
                  tasmota_plug("strip1_o1"), tasmota_plug("strip1_o2")]
    b.publish("lab/env/temperature", "22.4", retain=True)
    b.publish("lab/env/humidity", json.dumps({"value": 41.0, "unit": "%"}), retain=True)
    b.publish("stat/lamp/POWER", "ON", retain=True)
    return b


class FakeHomeAssistant(FakeApp):
    def __init__(self, token: str = "test-ha-token") -> None:
        super().__init__()
        self.token = token
        self.states: dict[str, dict[str, Any]] = {
            "light.bench": {"entity_id": "light.bench", "state": "off", "attributes": {"friendly_name": "Bench light"}},
            "sensor.lab_co2": {"entity_id": "sensor.lab_co2", "state": "612",
                               "attributes": {"unit_of_measurement": "ppm"}},
            "switch.fume_extractor": {"entity_id": "switch.fume_extractor", "state": "off", "attributes": {}},
        }
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def handle(self, req: FakeRequest) -> Response:
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"message": "401: Unauthorized"}
        if req.method == "GET" and req.path.startswith("/api/states/"):
            eid = req.path.removeprefix("/api/states/")
            s = self.states.get(eid)
            return (200, {**s, "last_changed": time.strftime("%Y-%m-%dT%H:%M:%SZ")}) if s else (404, {"message": "Entity not found."})
        if req.method == "POST" and req.path.startswith("/api/services/"):
            _, _, domain, service = req.path.strip("/").split("/")  # /api/services/<domain>/<service>
            data = req.json()
            self.calls.append((domain, service, data))
            ids = data.get("entity_id")
            ids = [ids] if isinstance(ids, str) else ids or []
            changed = []
            for eid in ids:
                if eid in self.states and service in ("turn_on", "turn_off", "toggle"):
                    cur = self.states[eid]["state"]
                    new = {"turn_on": "on", "turn_off": "off"}.get(service, "off" if cur == "on" else "on")
                    self.states[eid]["state"] = new
                    changed.append(self.states[eid])
            return 200, changed
        return 404, {"message": "not found"}
