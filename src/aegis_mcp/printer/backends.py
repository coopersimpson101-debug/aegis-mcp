"""OctoPrint and Moonraker (Klipper) REST backends with one normalized interface."""

from __future__ import annotations

import abc
from typing import Any
from urllib.parse import quote

import httpx

HEATERS = ("tool0", "tool1", "bed", "chamber")


class PrinterError(Exception):
    pass


def _status_template() -> dict[str, Any]:
    return {
        "backend": None,
        "state": "unknown",
        "operational": False,
        "printing": False,
        "paused": False,
        "temperatures": {},
        "job": {"file": None, "progress_pct": None, "print_time_s": None, "time_left_s": None},
    }


class PrinterBackend(abc.ABC):
    name = "abstract"

    def __init__(self, base_url: str, *, api_key: str | None = None, webcam_snapshot_url: str | None = None,
                 timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"X-Api-Key": api_key} if api_key else {}
        self.base_url = base_url.rstrip("/")
        self.webcam_snapshot_url = webcam_snapshot_url or f"{self.base_url}/webcam/?action=snapshot"
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise PrinterError(f"{self.name} unreachable: {exc}") from exc
        if r.status_code in (401, 403):
            raise PrinterError(f"{self.name} rejected credentials (HTTP {r.status_code}); check the API key")
        return r

    def _ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise PrinterError(f"{self.name} HTTP {r.status_code}: {r.text[:200]}")
        return r

    @abc.abstractmethod
    async def status(self) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def set_temperature(self, heater: str, target_c: float) -> None: ...
    @abc.abstractmethod
    async def upload(self, filename: str, data: bytes) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def list_files(self) -> list[dict[str, Any]]: ...
    @abc.abstractmethod
    async def start(self, filename: str) -> None: ...
    @abc.abstractmethod
    async def pause(self) -> None: ...
    @abc.abstractmethod
    async def resume(self) -> None: ...
    @abc.abstractmethod
    async def cancel(self) -> None: ...

    async def snapshot(self) -> bytes:
        r = self._ok(await self._req("GET", self.webcam_snapshot_url))
        return r.content


class OctoPrintBackend(PrinterBackend):
    """OctoPrint REST API (https://docs.octoprint.org/en/master/api/)."""

    name = "octoprint"

    async def status(self) -> dict[str, Any]:
        out = _status_template()
        out["backend"] = self.name
        r = await self._req("GET", "/api/printer")
        if r.status_code == 409:  # printer not connected to OctoPrint
            out["state"] = "disconnected"
        else:
            p = self._ok(r).json()
            flags = p.get("state", {}).get("flags", {})
            out["state"] = p.get("state", {}).get("text", "unknown")
            out["operational"] = bool(flags.get("operational"))
            out["printing"] = bool(flags.get("printing"))
            out["paused"] = bool(flags.get("paused"))
            for k, v in (p.get("temperature") or {}).items():
                if isinstance(v, dict) and "actual" in v:
                    out["temperatures"][k] = {"actual_c": v.get("actual"), "target_c": v.get("target")}
        j = await self._req("GET", "/api/job")
        if j.status_code < 400:
            job = j.json()
            out["job"] = {
                "file": ((job.get("job") or {}).get("file") or {}).get("name"),
                "progress_pct": (job.get("progress") or {}).get("completion"),
                "print_time_s": (job.get("progress") or {}).get("printTime"),
                "time_left_s": (job.get("progress") or {}).get("printTimeLeft"),
            }
        return out

    async def set_temperature(self, heater: str, target_c: float) -> None:
        if heater == "bed":
            body = {"command": "target", "target": target_c}
            url = "/api/printer/bed"
        elif heater == "chamber":
            body = {"command": "target", "target": target_c}
            url = "/api/printer/chamber"
        else:
            body = {"command": "target", "targets": {heater: target_c}}
            url = "/api/printer/tool"
        self._ok(await self._req("POST", url, json=body))

    async def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        r = self._ok(await self._req("POST", "/api/files/local", files={"file": (filename, data, "application/octet-stream")}))
        return {"name": filename, "bytes": len(data), "done": r.json().get("done", True)}

    async def list_files(self) -> list[dict[str, Any]]:
        r = self._ok(await self._req("GET", "/api/files", params={"recursive": "true"}))
        out = []

        def walk(items: list[dict[str, Any]]) -> None:
            for f in items:
                if f.get("type") == "folder":
                    walk(f.get("children", []))
                else:
                    out.append({"name": f.get("path") or f.get("name"), "size": f.get("size"), "date": f.get("date")})

        walk(r.json().get("files", []))
        return out

    async def start(self, filename: str) -> None:
        self._ok(await self._req("POST", f"/api/files/local/{quote(filename)}", json={"command": "select", "print": True}))

    async def pause(self) -> None:
        self._ok(await self._req("POST", "/api/job", json={"command": "pause", "action": "pause"}))

    async def resume(self) -> None:
        self._ok(await self._req("POST", "/api/job", json={"command": "pause", "action": "resume"}))

    async def cancel(self) -> None:
        self._ok(await self._req("POST", "/api/job", json={"command": "cancel"}))


class MoonrakerBackend(PrinterBackend):
    """Moonraker API for Klipper (https://moonraker.readthedocs.io/en/latest/web_api/)."""

    name = "moonraker"
    HEATER_MAP = {"tool0": "extruder", "tool1": "extruder1", "bed": "heater_bed", "chamber": "chamber"}

    async def status(self) -> dict[str, Any]:
        out = _status_template()
        out["backend"] = self.name
        r = self._ok(await self._req("GET", "/printer/objects/query?extruder&extruder1&heater_bed&print_stats&virtual_sdcard"))
        st = r.json()["result"]["status"]
        ps = st.get("print_stats", {})
        state = ps.get("state", "unknown")  # standby | printing | paused | complete | cancelled | error
        out["state"] = state
        out["operational"] = state not in ("error", "unknown")
        out["printing"] = state == "printing"
        out["paused"] = state == "paused"
        for ours, theirs in self.HEATER_MAP.items():
            h = st.get(theirs)
            if h:
                out["temperatures"][ours] = {"actual_c": h.get("temperature"), "target_c": h.get("target")}
        progress = (st.get("virtual_sdcard") or {}).get("progress")
        dur = ps.get("print_duration")
        left = None
        if progress and dur and progress > 0:
            left = max(0.0, dur / progress - dur)
        out["job"] = {
            "file": ps.get("filename") or None,
            "progress_pct": round(progress * 100, 2) if progress is not None else None,
            "print_time_s": dur,
            "time_left_s": left,
        }
        return out

    async def _gcode(self, script: str) -> None:
        self._ok(await self._req("POST", "/printer/gcode/script", params={"script": script}))

    async def set_temperature(self, heater: str, target_c: float) -> None:
        name = self.HEATER_MAP[heater]
        await self._gcode(f"SET_HEATER_TEMPERATURE HEATER={name} TARGET={target_c:g}")

    async def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        r = self._ok(await self._req("POST", "/server/files/upload", data={"root": "gcodes"},
                                     files={"file": (filename, data, "application/octet-stream")}))
        item = (r.json().get("result") or r.json()).get("item", {})
        return {"name": item.get("path", filename), "bytes": len(data), "done": True}

    async def list_files(self) -> list[dict[str, Any]]:
        r = self._ok(await self._req("GET", "/server/files/list", params={"root": "gcodes"}))
        return [{"name": f.get("path"), "size": f.get("size"), "date": f.get("modified")} for f in r.json()["result"]]

    async def start(self, filename: str) -> None:
        self._ok(await self._req("POST", "/printer/print/start", params={"filename": filename}))

    async def pause(self) -> None:
        self._ok(await self._req("POST", "/printer/print/pause"))

    async def resume(self) -> None:
        self._ok(await self._req("POST", "/printer/print/resume"))

    async def cancel(self) -> None:
        self._ok(await self._req("POST", "/printer/print/cancel"))


def make_backend(kind: str, base_url: str, **kw: Any) -> PrinterBackend:
    kinds = {"octoprint": OctoPrintBackend, "moonraker": MoonrakerBackend, "klipper": MoonrakerBackend}
    try:
        return kinds[kind.lower()](base_url, **kw)
    except KeyError as exc:
        raise ValueError(f"unknown printer backend {kind!r}; use octoprint or moonraker") from exc
