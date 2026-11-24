"""MQTT and Home Assistant transports for the lab server."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol

import httpx

MessageCallback = Callable[[str, str], None]


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard match (``+`` one level, ``#`` remaining levels)."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(p) == len(t)


class MqttTransport(Protocol):
    def connect(self) -> None: ...
    def publish(self, topic: str, payload: str, *, retain: bool = False, qos: int = 1) -> None: ...
    def subscribe(self, pattern: str) -> None: ...
    def latest(self, topic: str) -> tuple[str, float] | None: ...


class CachingMixin:
    """Keeps the latest payload per topic so sensor reads are instant."""

    def _init_cache(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}
        self._cache_lock = threading.Lock()

    def _store(self, topic: str, payload: str) -> None:
        with self._cache_lock:
            self._cache[topic] = (payload, time.time())

    def latest(self, topic: str) -> tuple[str, float] | None:
        with self._cache_lock:
            return self._cache.get(topic)


class PahoMqtt(CachingMixin):
    """Real broker connection via paho-mqtt (``pip install aegis-mcp[lab]``)."""

    def __init__(self, host: str, port: int = 1883, *, username: str | None = None, password: str | None = None,
                 client_id: str = "aegis-lab", tls: bool = False) -> None:
        self._init_cache()
        self.host, self.port = host, port
        self._subs: list[str] = []
        import paho.mqtt.client as mqtt  # deferred import: optional dependency

        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self._c.username_pw_set(username, password)
        if tls:
            self._c.tls_set()
        self._c.on_message = lambda _c, _u, msg: self._store(msg.topic, msg.payload.decode("utf-8", "replace"))
        self._c.on_connect = lambda c, *_a: [c.subscribe(s, qos=1) for s in self._subs]
        self._connected = False

    def connect(self) -> None:
        if not self._connected:
            self._c.connect(self.host, self.port, keepalive=30)
            self._c.loop_start()
            self._connected = True

    def subscribe(self, pattern: str) -> None:
        self._subs.append(pattern)
        if self._connected:
            self._c.subscribe(pattern, qos=1)

    def publish(self, topic: str, payload: str, *, retain: bool = False, qos: int = 1) -> None:
        self.connect()
        info = self._c.publish(topic, payload, qos=qos, retain=retain)
        info.wait_for_publish(timeout=5)


class HomeAssistant:
    """Minimal Home Assistant REST client (https://developers.home-assistant.io/docs/api/rest/)."""

    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=10,
                                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        r = await self._c.get(f"/api/states/{entity_id}")
        if r.status_code == 404:
            raise LookupError(f"entity {entity_id} not found")
        r.raise_for_status()
        s = r.json()
        return {"entity_id": s["entity_id"], "state": s["state"], "attributes": s.get("attributes", {}),
                "last_changed": s.get("last_changed")}

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        r = await self._c.post(f"/api/services/{domain}/{service}", json=data)
        r.raise_for_status()
        return [{"entity_id": s.get("entity_id"), "state": s.get("state")} for s in r.json()]
