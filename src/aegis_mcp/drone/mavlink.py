"""MAVLink backend (ArduPilot first-class, PX4 best-effort) using pymavlink.

Connection strings are pymavlink's: ``udpin:0.0.0.0:14550`` (SITL / telemetry radio via
MAVProxy), ``tcp:127.0.0.1:5760`` (ArduPilot SITL), ``COM5`` / ``/dev/ttyACM0`` (USB).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .base import DroneBackend, DroneError

try:  # optional dependency: pip install aegis-mcp[drone]
    from pymavlink import mavutil
except ImportError:  # pragma: no cover
    mavutil = None  # type: ignore[assignment]

FORCE_MAGIC = 21196  # ArduPilot/PX4 magic for forced arm/disarm
GUIDED_MODES = {"GUIDED", "OFFBOARD", "AUTO.LOITER", "POSHOLD"}


class MavlinkBackend(DroneBackend):
    name = "mavlink"

    def __init__(self, connection: str, *, baud: int = 57600, source_system: int = 250,
                 heartbeat_timeout_s: float = 10.0, ack_timeout_s: float = 5.0, guided_mode: str = "GUIDED") -> None:
        if mavutil is None:  # pragma: no cover
            raise RuntimeError("pymavlink is not installed: pip install 'aegis-mcp[drone]'")
        self.connection = connection
        self.baud = baud
        self.source_system = source_system
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.ack_timeout_s = ack_timeout_s
        self.guided_mode = guided_mode
        self.m: Any = None
        self._latest: dict[str, Any] = {}
        self._hb_time = 0.0
        self._acks: dict[int, int] = {}
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------ link
    def connect(self) -> None:
        if self.m is not None:
            return
        self.m = mavutil.mavlink_connection(self.connection, baud=self.baud, source_system=self.source_system,
                                            source_component=mavutil.mavlink.MAV_COMP_ID_MISSIONPLANNER)
        self._send_heartbeat()
        hb = self.m.wait_heartbeat(timeout=self.heartbeat_timeout_s)
        if hb is None:
            self.m.close()
            self.m = None
            raise DroneError(f"no heartbeat on {self.connection} within {self.heartbeat_timeout_s}s")
        self._on_msg(hb)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="mavlink-reader", daemon=True)
        self._reader.start()
        # give the vehicle a moment to stream position so the first telemetry call is useful
        deadline = time.monotonic() + 2.0
        with self._cond:
            while "GLOBAL_POSITION_INT" not in self._latest and time.monotonic() < deadline:
                self._cond.wait(0.1)

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=2)
        if self.m is not None:
            self.m.close()
            self.m = None

    def _send_heartbeat(self) -> None:
        with self._send_lock:
            self.m.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)

    def _on_msg(self, msg: Any) -> None:
        t = msg.get_type()
        if t == "BAD_DATA":
            return
        if t == "HEARTBEAT":
            if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
                return
            self._hb_time = time.monotonic()
        with self._cond:
            self._latest[t] = msg
            if t == "COMMAND_ACK":
                self._acks[msg.command] = msg.result
            self._cond.notify_all()

    def _read_loop(self) -> None:
        last_hb = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_hb > 1.0:  # GCS heartbeat keeps ArduPilot's GCS failsafe happy
                try:
                    self._send_heartbeat()
                except OSError:
                    pass
                last_hb = now
            try:
                msg = self.m.recv_match(blocking=True, timeout=0.2)
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            if msg is not None:
                self._on_msg(msg)

    def _require(self) -> None:
        if self.m is None:
            self.connect()

    # ------------------------------------------------------------------ commands
    def _command(self, cmd: int, *params: float, accept_in_progress: bool = True) -> None:
        self._require()
        p = list(params) + [0.0] * (7 - len(params))
        with self._cond:
            self._acks.pop(cmd, None)
        with self._send_lock:
            self.m.mav.command_long_send(self.m.target_system, self.m.target_component, cmd, 0, *p)
        deadline = time.monotonic() + self.ack_timeout_s
        with self._cond:
            while cmd not in self._acks:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DroneError(f"no COMMAND_ACK for {_cmd_name(cmd)} within {self.ack_timeout_s}s")
                self._cond.wait(left)
            result = self._acks.pop(cmd)
        ok = {mavutil.mavlink.MAV_RESULT_ACCEPTED}
        if accept_in_progress:
            ok.add(mavutil.mavlink.MAV_RESULT_IN_PROGRESS)
        if result not in ok:
            raise DroneError(f"{_cmd_name(cmd)} rejected by vehicle: {_result_name(result)}")

    def _current_mode(self) -> str | None:
        hb = self._latest.get("HEARTBEAT")
        return mavutil.mode_string_v10(hb) if hb is not None else None

    def set_mode(self, mode: str) -> None:
        self._require()
        mode = mode.upper()
        mapping = self.m.mode_mapping() or {}
        if mode not in mapping:
            raise DroneError(f"mode {mode!r} unknown for this autopilot; known: {sorted(mapping)}")
        mode_id = mapping[mode]
        if isinstance(mode_id, tuple):  # PX4: (base, main, sub)
            with self._send_lock:
                self.m.set_mode(mode)
        else:
            self._command(mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                          mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id)
        deadline = time.monotonic() + self.ack_timeout_s
        while time.monotonic() < deadline:
            if self._current_mode() == mode:
                return
            time.sleep(0.05)
        raise DroneError(f"vehicle did not enter {mode} (still {self._current_mode()})")

    def arm(self, force: bool = False) -> None:
        self._command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1, FORCE_MAGIC if force else 0)
        self._wait(lambda: self.telemetry()["armed"], "armed")

    def disarm(self, force: bool = False) -> None:
        self._command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, FORCE_MAGIC if force else 0)
        self._wait(lambda: not self.telemetry()["armed"], "disarmed")

    def takeoff(self, alt_m: float) -> None:
        if self._current_mode() not in GUIDED_MODES:
            self.set_mode(self.guided_mode)
        self._command(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, float("nan"), float("nan"), float("nan"), alt_m)

    def land(self) -> None:
        self._command(mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0, float("nan"), float("nan"), float("nan"), 0)

    def rtl(self) -> None:
        self._command(mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH)

    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        self._require()
        type_mask = 0b0000_1111_1111_1000  # position only
        with self._send_lock:
            self.m.mav.set_position_target_global_int_send(
                0, self.m.target_system, self.m.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, type_mask,
                int(lat * 1e7), int(lon * 1e7), alt_m, 0, 0, 0, 0, 0, 0, 0, 0)

    def _wait(self, pred: Any, what: str) -> None:
        deadline = time.monotonic() + self.ack_timeout_s
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.05)
        raise DroneError(f"vehicle acknowledged but did not become {what}")

    # ------------------------------------------------------------------ telemetry
    def telemetry(self) -> dict[str, Any]:
        self._require()
        with self._cond:
            hb = self._latest.get("HEARTBEAT")
            pos = self._latest.get("GLOBAL_POSITION_INT")
            sys_ = self._latest.get("SYS_STATUS")
            gps = self._latest.get("GPS_RAW_INT")
            hud = self._latest.get("VFR_HUD")
            ext = self._latest.get("EXTENDED_SYS_STATE")
        armed = bool(hb and hb.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        rel_alt = pos.relative_alt / 1000.0 if pos else None
        in_air = None
        if ext is not None:
            in_air = ext.landed_state in (mavutil.mavlink.MAV_LANDED_STATE_IN_AIR, mavutil.mavlink.MAV_LANDED_STATE_TAKEOFF,
                                          mavutil.mavlink.MAV_LANDED_STATE_LANDING)
        elif rel_alt is not None:
            in_air = armed and rel_alt > 0.5
        return {
            "backend": self.name,
            "connected": hb is not None,
            "autopilot": _enum_name("MAV_AUTOPILOT", hb.autopilot) if hb else None,
            "vehicle_type": _enum_name("MAV_TYPE", hb.type) if hb else None,
            "mode": mavutil.mode_string_v10(hb) if hb else None,
            "armed": armed,
            "in_air": in_air,
            "lat": pos.lat / 1e7 if pos else None,
            "lon": pos.lon / 1e7 if pos else None,
            "rel_alt_m": rel_alt,
            "heading_deg": pos.hdg / 100.0 if pos and pos.hdg != 65535 else None,
            "groundspeed_mps": hud.groundspeed if hud else None,
            "battery_v": sys_.voltage_battery / 1000.0 if sys_ and sys_.voltage_battery != 65535 else None,
            "battery_pct": sys_.battery_remaining if sys_ and sys_.battery_remaining >= 0 else None,
            "gps_fix": gps.fix_type if gps else None,
            "satellites": gps.satellites_visible if gps else None,
            "last_heartbeat_age_s": round(time.monotonic() - self._hb_time, 2) if self._hb_time else None,
        }


def _enum_name(enum: str, value: int) -> str:
    try:
        return mavutil.mavlink.enums[enum][value].name
    except KeyError:
        return str(value)


def _cmd_name(cmd: int) -> str:
    return _enum_name("MAV_CMD", cmd)


def _result_name(result: int) -> str:
    return _enum_name("MAV_RESULT", result)
