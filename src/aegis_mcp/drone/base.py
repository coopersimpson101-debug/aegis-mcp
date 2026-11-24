"""Backend interface every drone link implements. Methods are synchronous (serial/UDP I/O);
the MCP server runs them in a worker thread."""

from __future__ import annotations

import abc
from typing import Any


class DroneError(Exception):
    """Vehicle rejected a command or the link failed."""


class DroneBackend(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:  # noqa: B027  # optional hook, default no-op
        pass

    @abc.abstractmethod
    def telemetry(self) -> dict[str, Any]:
        """Normalized dict: lat, lon, rel_alt_m, heading_deg, groundspeed_mps, armed, mode,
        battery_v, battery_pct, gps_fix, satellites, last_heartbeat_age_s, in_air."""

    @abc.abstractmethod
    def arm(self, force: bool = False) -> None: ...
    @abc.abstractmethod
    def disarm(self, force: bool = False) -> None: ...
    @abc.abstractmethod
    def set_mode(self, mode: str) -> None: ...
    @abc.abstractmethod
    def takeoff(self, alt_m: float) -> None: ...
    @abc.abstractmethod
    def land(self) -> None: ...
    @abc.abstractmethod
    def rtl(self) -> None: ...
    @abc.abstractmethod
    def goto(self, lat: float, lon: float, alt_m: float) -> None: ...
