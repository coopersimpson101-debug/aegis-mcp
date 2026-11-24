"""Fake OctoPrint and Moonraker hosts with a shared toy printer model.

Heaters move 40% of the way to their target on every status poll; a running print advances
10% per poll. Good enough to exercise every code path without a real printer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .http_fake import TINY_JPEG, FakeApp, FakeRequest, Response


@dataclass
class ToyPrinter:
    temps: dict[str, list[float]] = field(default_factory=lambda: {"tool0": [21.0, 0.0], "bed": [21.0, 0.0]})
    files: dict[str, bytes] = field(default_factory=lambda: {"cube.gcode": b"G28\nM104 S200\nG1 X10\n"})
    state: str = "idle"  # idle | printing | paused | cancelled | complete
    job_file: str | None = None
    progress: float = 0.0
    started: float = 0.0

    def tick(self) -> None:
        for v in self.temps.values():
            v[0] = round(v[0] + 0.4 * (v[1] - v[0] if v[1] > 0 else 21.0 - v[0]), 2)
        if self.state == "printing":
            self.progress = min(1.0, self.progress + 0.1)
            if self.progress >= 1.0:
                self.state = "complete"

    def start(self, name: str) -> bool:
        if name not in self.files or self.state in ("printing", "paused"):
            return False
        self.state, self.job_file, self.progress, self.started = "printing", name, 0.0, time.time()
        return True


class FakeOctoPrint(FakeApp):
    def __init__(self, api_key: str | None = "test-octo-key", printer: ToyPrinter | None = None) -> None:
        super().__init__(api_key=api_key)
        self.printer = printer or ToyPrinter()

    def handle(self, req: FakeRequest) -> Response:
        p = self.printer
        if req.path.startswith("/webcam"):
            return 200, TINY_JPEG
        if not self.authorized(req):
            return 403, {"error": "Invalid API key"}
        if req.method == "GET" and req.path == "/api/printer":
            p.tick()
            text = {"idle": "Operational", "printing": "Printing", "paused": "Paused",
                    "cancelled": "Operational", "complete": "Operational"}[p.state]
            return 200, {
                "temperature": {k: {"actual": a, "target": t, "offset": 0} for k, (a, t) in p.temps.items()},
                "state": {"text": text, "flags": {"operational": True, "printing": p.state == "printing",
                                                  "paused": p.state == "paused", "ready": p.state != "printing"}},
            }
        if req.method == "GET" and req.path == "/api/job":
            el = time.time() - p.started if p.job_file else None
            return 200, {"job": {"file": {"name": p.job_file}},
                         "progress": {"completion": p.progress * 100 if p.job_file else None,
                                      "printTime": el, "printTimeLeft": 600 * (1 - p.progress) if p.job_file else None},
                         "state": p.state}
        if req.method == "POST" and req.path == "/api/printer/tool":
            body = req.json()
            if body.get("command") != "target":
                return 400, {"error": "bad command"}
            for k, v in body["targets"].items():
                p.temps.setdefault(k, [21.0, 0.0])[1] = float(v)
            return 204, b""
        if req.method == "POST" and req.path == "/api/printer/bed":
            p.temps["bed"][1] = float(req.json()["target"])
            return 204, b""
        if req.method == "POST" and req.path == "/api/files/local":
            fname, data = req.form()["file"]
            p.files[fname] = data
            return 201, {"done": True, "files": {"local": {"name": fname}}}
        if req.method == "GET" and req.path == "/api/files":
            return 200, {"files": [{"name": n, "path": n, "type": "machinecode", "size": len(d), "date": 0}
                                   for n, d in p.files.items()]}
        if req.method == "POST" and req.path.startswith("/api/files/local/"):
            name = req.path.removeprefix("/api/files/local/")
            from urllib.parse import unquote
            name = unquote(name)
            if name not in p.files:
                return 404, {"error": "file not found"}
            if req.json().get("print") and not p.start(name):
                return 409, {"error": "printer busy"}
            return 204, b""
        if req.method == "POST" and req.path == "/api/job":
            body = req.json()
            if body["command"] == "cancel":
                if p.state not in ("printing", "paused"):
                    return 409, {"error": "no active job"}
                p.state = "cancelled"
            elif body["command"] == "pause":
                if body.get("action") == "pause" and p.state == "printing":
                    p.state = "paused"
                elif body.get("action") == "resume" and p.state == "paused":
                    p.state = "printing"
                else:
                    return 409, {"error": f"cannot {body.get('action')} while {p.state}"}
            return 204, b""
        return 404, {"error": f"no route {req.method} {req.path}"}


class FakeMoonraker(FakeApp):
    def __init__(self, api_key: str | None = None, printer: ToyPrinter | None = None) -> None:
        super().__init__(api_key=api_key)
        self.printer = printer or ToyPrinter()
        self.gcode_log: list[str] = []

    def handle(self, req: FakeRequest) -> Response:
        p = self.printer
        if req.path.startswith("/webcam"):
            return 200, TINY_JPEG
        if not self.authorized(req):
            return 401, {"error": {"message": "Unauthorized"}}
        if req.path == "/printer/objects/query":
            p.tick()
            state = {"idle": "standby", "printing": "printing", "paused": "paused",
                     "cancelled": "cancelled", "complete": "complete"}[p.state]
            st: dict[str, Any] = {
                "extruder": {"temperature": p.temps["tool0"][0], "target": p.temps["tool0"][1]},
                "heater_bed": {"temperature": p.temps["bed"][0], "target": p.temps["bed"][1]},
                "print_stats": {"state": state, "filename": p.job_file or "",
                                "print_duration": 60.0 * p.progress * 10 if p.job_file else 0.0},
                "virtual_sdcard": {"progress": p.progress},
            }
            wanted = [k for k in req.query] or list(st)
            return 200, {"result": {"eventtime": time.time(), "status": {k: st[k] for k in wanted if k in st}}}
        if req.method == "POST" and req.path == "/printer/gcode/script":
            script = req.q("script") or ""
            self.gcode_log.append(script)
            if script.startswith("SET_HEATER_TEMPERATURE"):
                parts = dict(kv.split("=") for kv in script.split()[1:])
                key = {"extruder": "tool0", "heater_bed": "bed"}.get(parts["HEATER"], parts["HEATER"])
                p.temps.setdefault(key, [21.0, 0.0])[1] = float(parts["TARGET"])
            return 200, {"result": "ok"}
        if req.method == "POST" and req.path == "/server/files/upload":
            form = req.form()
            fname, data = form["file"]
            p.files[fname] = data
            return 201, {"result": {"item": {"path": fname, "root": form.get("root", "gcodes")}, "action": "create_file"}}
        if req.method == "GET" and req.path == "/server/files/list":
            return 200, {"result": [{"path": n, "size": len(d), "modified": 0.0} for n, d in p.files.items()]}
        if req.method == "POST" and req.path == "/printer/print/start":
            if not p.start(req.q("filename") or ""):
                return 400, {"error": {"message": "cannot start"}}
            return 200, {"result": "ok"}
        if req.method == "POST" and req.path == "/printer/print/pause":
            if p.state != "printing":
                return 400, {"error": {"message": "not printing"}}
            p.state = "paused"
            return 200, {"result": "ok"}
        if req.method == "POST" and req.path == "/printer/print/resume":
            if p.state != "paused":
                return 400, {"error": {"message": "not paused"}}
            p.state = "printing"
            return 200, {"result": "ok"}
        if req.method == "POST" and req.path == "/printer/print/cancel":
            p.state = "cancelled"
            return 200, {"result": "ok"}
        return 404, {"error": {"message": f"no route {req.path}"}}
