"""Risk levels shared by every server and by the orchestrator."""

from __future__ import annotations

from enum import IntEnum


class Risk(IntEnum):
    """How bad is it if this call is wrong?

    READ      - observes only (status, telemetry, listing).
    LOW       - changes state but is a *safe direction* (cool down, pause, land, RTL).
    MEDIUM    - normal actuation (set a moderate temperature, toggle a lamp, goto inside fence).
    HIGH      - can damage hardware, property or data (arm motors, heat bed hot, delete files).
    CRITICAL  - irreversible or dangerous to people (force-disarm in flight, permanent delete).
    """

    READ = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: "Risk | str | int") -> "Risk":
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown risk level: {value!r}") from exc

    @property
    def label(self) -> str:
        return self.name.lower()


RISK_META_KEY = "aegis/risk"
"""Key used in MCP tool ``_meta`` so clients (like the orchestrator) learn each tool's base risk."""
