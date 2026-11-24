"""In-repo fake vehicles.

* :class:`FakeMavlinkVehicle` - speaks real MAVLink (pymavlink encoder) over TCP and
  behaves like a much-simplified ArduCopter: heartbeats, position, battery, GPS, arm checks,
  GUIDED takeoff / goto, LAND, RTL. It is *not* a flight dynamics model - use ArduPilot SITL
  for that (see docs/simulators.md).
* :class:`FakeAegisLinkFC` - an in-memory transport implementing the AEGIS-Link protocol.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from typing import Any

from ..drone.aegislink import decode, encode

COPTER_MODES = {"STABILIZE": 0, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4, "LOITER": 5, "RTL": 6, "LAND": 9}
COPTER_MODE_NAMES = {v: k for k, v in COPTER_MODES.items()}


class _Kinematics:
    """Shared toy motion model: fly straight at fixed speeds toward a target."""

    def __init__(self, home_lat: float, home_lon: float) -> None:
        self.home = (home_lat, home_lon)
        self.lat, self.lon, self.alt = home_lat, home_lon, 0.0
        self.armed = False
        self.mode = "STABILIZE"
        self.target: tuple[float, float, float] | None = None
        self.battery_v, self.battery_pct = 16.6, 100
        self.gps_fix, self.sats = 3, 14
        self.h_speed, self.v_speed = 15.0, 4.0

    @property
    def in_air(self) -> bool:
        return self.alt > 0.3

    def step(self, dt: float) -> None:
        if self.mode == "LAND":
            self.target = (self.lat, self.lon, 0.0)
        elif self.mode == "RTL":
            self.target = (self.home[0], self.home[1], self.alt if self._dist_to(*self.home) > 1 else 0.0)
        if self.target and self.armed:
            tlat, tlon, talt = self.target
            d = self._dist_to(tlat, tlon)
            step = self.h_speed * dt
            if d <= step or d < 0.01:
                self.lat, self.lon = tlat, tlon
            else:
                f = step / d
                self.lat += (tlat - self.lat) * f
                self.lon += (tlon - self.lon) * f
            dz = talt - self.alt
            self.alt += max(-self.v_speed * dt, min(self.v_speed * dt, dz))
            if abs(self.alt - talt) < 0.05:
                self.alt = talt
        at_spot = self.mode == "LAND" or self._dist_to(*self.home) < 1.0
        if self.armed and self.mode in ("LAND", "RTL") and self.alt <= 0.0 and at_spot:
            self.armed = False  # auto-disarm on touchdown
            self.mode = "LAND" if self.mode == "LAND" else "STABILIZE"
            self.target = None

    def _dist_to(self, lat: float, lon: float) -> float:
        dy = (lat - self.lat) * 111_320.0
        dx = (lon - self.lon) * 111_320.0 * math.cos(math.radians(self.lat))
        return math.hypot(dx, dy)


class FakeMavlinkVehicle:
    def __init__(self, port: int = 0, *, home_lat: float = 47.397742, home_lon: float = 8.545594,
                 rate_hz: float = 20.0) -> None:
        import socket

        from pymavlink import mavutil

        self.mavutil = mavutil
        if port == 0:  # pick a free port
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
        self.port = port
        self.k = _Kinematics(home_lat, home_lon)
        self.rate_hz = rate_hz
        self.conn = mavutil.mavlink_connection(f"tcpin:127.0.0.1:{port}", source_system=1, source_component=1)
        self.received: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-vehicle", daemon=True)

    @property
    def connection_string(self) -> str:
        return f"tcp:127.0.0.1:{self.port}"

    def start(self) -> "FakeMavlinkVehicle":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.conn.close()

    def __enter__(self) -> "FakeMavlinkVehicle":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        dt = 1.0 / self.rate_hz
        last = time.monotonic()
        while not self._stop.is_set():
            while True:
                try:
                    msg = self.conn.recv_msg()
                except (OSError, ValueError):
                    msg = None
                if msg is None:
                    break
                self._handle(msg)
            now = time.monotonic()
            self.k.step(now - last)
            last = now
            if self.conn.port is not None:
                try:
                    self._send_state()
                except OSError:
                    pass
            time.sleep(dt)

    def _send_state(self) -> None:
        m = self.mavutil.mavlink
        k = self.k
        base = m.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (m.MAV_MODE_FLAG_SAFETY_ARMED if k.armed else 0)
        status = m.MAV_STATE_ACTIVE if k.armed else m.MAV_STATE_STANDBY
        self.conn.mav.heartbeat_send(m.MAV_TYPE_QUADROTOR, m.MAV_AUTOPILOT_ARDUPILOTMEGA, base,
                                     COPTER_MODES[k.mode], status)
        ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
        self.conn.mav.global_position_int_send(ms, int(k.lat * 1e7), int(k.lon * 1e7), int((488 + k.alt) * 1000),
                                               int(k.alt * 1000), 0, 0, 0, 0)
        self.conn.mav.sys_status_send(0, 0, 0, 500, int(k.battery_v * 1000), -1, k.battery_pct, 0, 0, 0, 0, 0, 0)
        self.conn.mav.gps_raw_int_send(ms * 1000, k.gps_fix, int(k.lat * 1e7), int(k.lon * 1e7), 0, 80, 120, 0, 0,
                                       k.sats)
        landed = m.MAV_LANDED_STATE_IN_AIR if k.in_air else m.MAV_LANDED_STATE_ON_GROUND
        self.conn.mav.extended_sys_state_send(0, landed)

    def _ack(self, cmd: int, result: int) -> None:
        self.conn.mav.command_ack_send(cmd, result)

    def _handle(self, msg: Any) -> None:
        m = self.mavutil.mavlink
        k = self.k
        t = msg.get_type()
        self.received.append(t)
        if t == "COMMAND_LONG":
            c = msg.command
            if c == m.MAV_CMD_COMPONENT_ARM_DISARM:
                if msg.param1 == 1:
                    if k.gps_fix < 3:
                        return self._ack(c, m.MAV_RESULT_FAILED)
                    k.armed = True
                else:
                    if k.in_air and int(msg.param2) != 21196:
                        return self._ack(c, m.MAV_RESULT_DENIED)
                    k.armed = False
                    k.alt = 0.0 if not k.in_air else k.alt
                return self._ack(c, m.MAV_RESULT_ACCEPTED)
            if c == m.MAV_CMD_DO_SET_MODE:
                name = COPTER_MODE_NAMES.get(int(msg.param2))
                if name is None:
                    return self._ack(c, m.MAV_RESULT_DENIED)
                k.mode = name
                if name == "GUIDED":
                    k.target = None
                return self._ack(c, m.MAV_RESULT_ACCEPTED)
            if c == m.MAV_CMD_NAV_TAKEOFF:
                if not k.armed or k.mode != "GUIDED":
                    return self._ack(c, m.MAV_RESULT_DENIED)
                k.target = (k.lat, k.lon, float(msg.param7))
                return self._ack(c, m.MAV_RESULT_ACCEPTED)
            if c == m.MAV_CMD_NAV_LAND:
                k.mode = "LAND"
                return self._ack(c, m.MAV_RESULT_ACCEPTED)
            if c == m.MAV_CMD_NAV_RETURN_TO_LAUNCH:
                k.mode = "RTL"
                return self._ack(c, m.MAV_RESULT_ACCEPTED)
            return self._ack(c, m.MAV_RESULT_UNSUPPORTED)
        if t == "SET_MODE":
            name = COPTER_MODE_NAMES.get(msg.custom_mode)
            if name:
                k.mode = name
        elif t == "SET_POSITION_TARGET_GLOBAL_INT":
            if k.armed and k.mode == "GUIDED" and k.in_air:
                k.target = (msg.lat_int / 1e7, msg.lon_int / 1e7, msg.alt)


class FakeAegisLinkFC:
    """In-memory LinkTransport that answers like a custom FC firmware would."""

    firmware = "skyfc-sim-1.0"

    def __init__(self, *, home_lat: float = 47.397742, home_lon: float = 8.545594, drop_first: int = 0,
                 corrupt_first: int = 0) -> None:
        self.k = _Kinematics(home_lat, home_lon)
        self.k.mode = "IDLE"
        self._rx: "queue.Queue[bytes]" = queue.Queue()
        self.frames: list[str] = []
        self.drop_first = drop_first
        self.corrupt_first = corrupt_first
        self._last = time.monotonic()

    # LinkTransport ------------------------------------------------------------
    def write(self, data: bytes) -> int:
        now = time.monotonic()
        self.k.step(now - self._last)
        self._last = now
        seq, cmd, args = decode(data)
        self.frames.append(cmd)
        if self.drop_first > 0:
            self.drop_first -= 1
            return len(data)
        reply = self._reply(seq, cmd, args)
        if self.corrupt_first > 0:
            self.corrupt_first -= 1
            reply = reply[:-4] + b"00\r\n"
        self._rx.put(reply)
        return len(data)

    def readline(self) -> bytes:
        try:
            return self._rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def _reply(self, seq: int, cmd: str, a: list[str]) -> bytes:
        k = self.k
        nak = lambda why: encode(seq, "NAK", cmd, why)  # noqa: E731
        ack = encode(seq, "ACK", cmd)
        if cmd == "PING":
            return encode(seq, "PONG", self.firmware)
        if cmd == "TELEM":
            return encode(seq, "TEL", round(k.lat, 7), round(k.lon, 7), round(k.alt, 2), k.armed, k.mode,
                          k.battery_v, k.battery_pct, k.sats, k.gps_fix, k.in_air)
        if cmd == "ARM":
            if k.gps_fix < 3:
                return nak("NO_GPS")
            k.armed, k.mode = True, "GUIDED"
            return ack
        if cmd == "DISARM":
            if k.in_air and "FORCE" not in a:
                return nak("IN_AIR")
            k.armed = False
            return ack
        if cmd == "MODE":
            k.mode = a[0]
            return ack
        if cmd == "TAKEOFF":
            if not k.armed:
                return nak("NOT_ARMED")
            k.mode, k.target = "GUIDED", (k.lat, k.lon, float(a[0]))
            return ack
        if cmd == "LAND":
            k.mode = "LAND"
            return ack
        if cmd == "RTL":
            k.mode = "RTL"
            return ack
        if cmd == "GOTO":
            if not (k.armed and k.in_air):
                return nak("NOT_FLYING")
            k.target = (float(a[0]), float(a[1]), float(a[2]))
            return ack
        return nak("UNKNOWN_CMD")
