"""SMART health via ``smartctl --json`` (smartmontools >= 7.0)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable

Runner = Callable[[list[str]], str]
_DEVICE = re.compile(r"^(/dev/[A-Za-z0-9/_-]+|[A-Za-z0-9_-]+)$")

ATA_WATCH = {5: "reallocated_sectors", 187: "reported_uncorrectable", 188: "command_timeout",
             197: "pending_sectors", 198: "offline_uncorrectable", 199: "udma_crc_errors"}


class SmartError(Exception):
    pass


def default_runner(smartctl: str = "smartctl") -> Runner:
    def run(args: list[str]) -> str:
        exe = shutil.which(smartctl)
        if exe is None:
            raise SmartError("smartctl not found; install smartmontools on the NAS host")
        # smartctl's exit status is a bitmask that is non-zero for healthy-but-noteworthy disks,
        # so we parse stdout regardless and only fail on missing JSON.
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60)
        if not proc.stdout.strip():
            raise SmartError(f"smartctl produced no output (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
        return proc.stdout
    return run


def validate_device(dev: str) -> str:
    if not _DEVICE.match(dev) or ".." in dev:
        raise SmartError(f"invalid device name {dev!r}")
    return dev


def scan(runner: Runner) -> list[dict[str, Any]]:
    data = json.loads(runner(["--scan", "--json"]))
    return [{"name": d.get("name"), "type": d.get("type"), "info": d.get("info_name")} for d in data.get("devices", [])]


def parse_smartctl(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce ``smartctl -a --json`` output to the handful of numbers that predict failure."""
    dev = data.get("device", {})
    out: dict[str, Any] = {
        "device": dev.get("name"),
        "protocol": dev.get("protocol"),
        "model": data.get("model_name") or data.get("model_family"),
        "serial": data.get("serial_number"),
        "capacity_bytes": (data.get("user_capacity") or {}).get("bytes"),
        "smart_passed": (data.get("smart_status") or {}).get("passed"),
        "temperature_c": (data.get("temperature") or {}).get("current"),
        "power_on_hours": (data.get("power_on_time") or {}).get("hours"),
        "counters": {},
        "warnings": [],
    }
    for row in (data.get("ata_smart_attributes") or {}).get("table", []):
        key = ATA_WATCH.get(row.get("id"))
        if key:
            out["counters"][key] = (row.get("raw") or {}).get("value")
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        out["counters"].update({
            "percentage_used": nvme.get("percentage_used"),
            "media_errors": nvme.get("media_errors"),
            "critical_warning": nvme.get("critical_warning"),
            "unsafe_shutdowns": nvme.get("unsafe_shutdowns"),
        })
    c = out["counters"]
    for k in ("reallocated_sectors", "pending_sectors", "offline_uncorrectable", "reported_uncorrectable", "media_errors"):
        if (c.get(k) or 0) > 0:
            out["warnings"].append(f"{k}={c[k]}")
    if (c.get("critical_warning") or 0) != 0:
        out["warnings"].append(f"nvme critical_warning={c['critical_warning']}")
    if (c.get("percentage_used") or 0) >= 90:
        out["warnings"].append(f"nvme wear {c['percentage_used']}%")
    if (out["temperature_c"] or 0) >= 55:
        out["warnings"].append(f"hot: {out['temperature_c']} C")
    for m in (data.get("smartctl") or {}).get("messages", []):
        if m.get("severity") == "error":
            out["warnings"].append("smartctl: " + m.get("string", ""))
    if out["smart_passed"] is False:
        out["health"] = "FAILING"
    elif out["warnings"]:
        out["health"] = "WARNING"
    elif out["smart_passed"] is None:
        out["health"] = "UNKNOWN"
    else:
        out["health"] = "OK"
    return out


def device_health(runner: Runner, device: str, dev_type: str | None = None) -> dict[str, Any]:
    args = ["-a", "--json"]
    if dev_type:
        if not re.fullmatch(r"[a-z0-9,]+", dev_type):
            raise SmartError(f"invalid device type {dev_type!r}")
        args += ["-d", dev_type]
    return parse_smartctl(json.loads(runner([*args, validate_device(device)])))
