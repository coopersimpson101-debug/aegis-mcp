"""AEGIS-Link v1: a tiny line protocol for custom flight-controller firmware.

Use this when your own FC firmware does not speak MAVLink. The full spec lives in
``docs/aegis-link-protocol.md``; summary::

    $AGL,<seq>,<CMD>[,<arg>...]*<CK>\\r\\n

``seq`` is 0-65535 (wraps), ``CK`` is the two-hex-digit XOR of every byte between ``$`` and
``*`` (NMEA style). Host -> FC commands: ``PING``, ``TELEM``, ``ARM[,FORCE]``,
``DISARM[,FORCE]``, ``MODE,<name>``, ``TAKEOFF,<alt_m>``, ``LAND``, ``RTL``,
``GOTO,<lat>,<lon>,<alt_m>``. FC -> host replies echo ``seq``: ``ACK,<CMD>``,
``NAK,<CMD>,<reason>``, ``PONG,<fw_version>`` and
``TEL,<lat>,<lon>,<rel_alt_m>,<armed 0|1>,<mode>,<batt_v>,<batt_pct>,<sats>,<fix>,<in_air 0|1>``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

from .base import DroneBackend, DroneError


class LinkTransport(Protocol):
    def write(self, data: bytes) -> Any: ...
    def readline(self) -> bytes: ...


def checksum(body: str) -> str:
    ck = 0
    for b in body.encode("ascii"):
        ck ^= b
    return f"{ck:02X}"


def encode(seq: int, cmd: str, *args: Any) -> bytes:
    fields = ["AGL", str(seq % 65536), cmd, *(_fmt(a) for a in args)]
    for f in fields:
        if any(c in f for c in ",*$\r\n"):
            raise ValueError(f"illegal character in field {f!r}")
    body = ",".join(fields)
    return f"${body}*{checksum(body)}\r\n".encode("ascii")


def _fmt(a: Any) -> str:
    if isinstance(a, bool):
        return "1" if a else "0"
    if isinstance(a, float):
        return f"{a:.7f}".rstrip("0").rstrip(".")
    return str(a)


def decode(line: bytes) -> tuple[int, str, list[str]]:
    text = line.decode("ascii", "replace").strip()
    if not text.startswith("$") or "*" not in text:
        raise DroneError(f"malformed frame: {text!r}")
    body, ck = text[1:].rsplit("*", 1)
    if checksum(body).upper() != ck.upper():
        raise DroneError(f"bad checksum on frame: {text!r}")
    fields = body.split(",")
    if len(fields) < 3 or fields[0] != "AGL":
        raise DroneError(f"not an AEGIS-Link frame: {text!r}")
    return int(fields[1]), fields[2], fields[3:]


class AegisLinkBackend(DroneBackend):
    name = "aegis-link"

    def __init__(self, port: str | None = None, *, baud: int = 115200, transport: LinkTransport | None = None,
                 timeout_s: float = 2.0, retries: int = 2) -> None:
        self.port = port
        self.baud = baud
        self.transport = transport
        self.timeout_s = timeout_s
        self.retries = retries
        self._seq = 0
        self._lock = threading.Lock()
        self.firmware: str | None = None
        self._connected = False

    def connect(self) -> None:
        if self.transport is None:
            if not self.port:
                raise DroneError("AEGIS-Link needs a serial port, e.g. COM7 or /dev/ttyUSB0")
            import serial  # pyserial

            self.transport = serial.serial_for_url(self.port, baudrate=self.baud, timeout=self.timeout_s)
        self._connected = True
        try:
            _, args = self._transact("PING")
        except DroneError:
            self._connected = False
            raise
        self.firmware = args[0] if args else None

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def _transact(self, cmd: str, *args: Any) -> tuple[str, list[str]]:
        if not self._connected:
            self.connect()
        with self._lock:
            last_err: Exception | None = None
            for _ in range(self.retries + 1):
                self._seq = (self._seq + 1) % 65536
                seq = self._seq
                self.transport.write(encode(seq, cmd, *args))  # type: ignore[union-attr]
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    raw = self.transport.readline()  # type: ignore[union-attr]
                    if not raw:
                        continue
                    try:
                        rseq, kind, rargs = decode(raw)
                    except DroneError as exc:
                        last_err = exc
                        continue
                    if rseq != seq:
                        continue  # stale reply to an earlier retry
                    if kind == "NAK":
                        reason = rargs[1] if len(rargs) > 1 else "unspecified"
                        raise DroneError(f"FC rejected {cmd}: {reason}")
                    return kind, rargs
                last_err = DroneError(f"timeout waiting for reply to {cmd}")
            raise last_err or DroneError(f"no reply to {cmd}")

    def telemetry(self) -> dict[str, Any]:
        kind, a = self._transact("TELEM")
        if kind != "TEL" or len(a) < 10:
            raise DroneError(f"unexpected telemetry reply {kind} {a}")
        return {
            "backend": self.name,
            "connected": True,
            "firmware": self.firmware,
            "lat": float(a[0]), "lon": float(a[1]), "rel_alt_m": float(a[2]),
            "armed": a[3] == "1", "mode": a[4],
            "battery_v": float(a[5]), "battery_pct": int(a[6]),
            "satellites": int(a[7]), "gps_fix": int(a[8]), "in_air": a[9] == "1",
            "heading_deg": None, "groundspeed_mps": None, "last_heartbeat_age_s": 0.0,
        }

    def _ack(self, cmd: str, *args: Any) -> None:
        kind, _ = self._transact(cmd, *args)
        if kind != "ACK":
            raise DroneError(f"unexpected reply {kind} to {cmd}")

    def arm(self, force: bool = False) -> None:
        self._ack("ARM", *(["FORCE"] if force else []))

    def disarm(self, force: bool = False) -> None:
        self._ack("DISARM", *(["FORCE"] if force else []))

    def set_mode(self, mode: str) -> None:
        self._ack("MODE", mode.upper())

    def takeoff(self, alt_m: float) -> None:
        self._ack("TAKEOFF", float(alt_m))

    def land(self) -> None:
        self._ack("LAND")

    def rtl(self) -> None:
        self._ack("RTL")

    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        self._ack("GOTO", float(lat), float(lon), float(alt_m))
