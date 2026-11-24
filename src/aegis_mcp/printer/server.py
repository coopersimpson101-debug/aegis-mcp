"""MCP server for 3D printers (OctoPrint or Moonraker/Klipper)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from typing import Annotated, Any, Literal, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import BaseModel, Field

from ..common import cli_main, make_guard, tool_kwargs
from ..safety import Guard, Risk, SafetyViolation
from .backends import PrinterBackend, PrinterError, make_backend

Heater = Literal["tool0", "tool1", "bed", "chamber"]
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.(gcode|gco|g|bgcode)$", re.IGNORECASE)
_TEMP_CMD = re.compile(r"^\s*(M104|M109|M140|M190|M141|M191)\b[^;]*?\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)


class PrinterLimits(BaseModel):
    max_hotend_c: float = 285.0
    max_bed_c: float = 110.0
    max_chamber_c: float = 70.0
    confirm_hotend_above_c: float = 250.0
    confirm_bed_above_c: float = 90.0
    max_upload_bytes: int = 20_000_000

    def hard_max(self, heater: str) -> float:
        return {"bed": self.max_bed_c, "chamber": self.max_chamber_c}.get(heater, self.max_hotend_c)

    def confirm_above(self, heater: str) -> float:
        return {"bed": self.confirm_bed_above_c, "chamber": self.max_chamber_c}.get(heater, self.confirm_hotend_above_c)


def scan_gcode(text: str) -> dict[str, float]:
    """Return the highest temperature each heater class is commanded to in a G-code file."""
    peaks = {"hotend": 0.0, "bed": 0.0, "chamber": 0.0}
    for line in text.splitlines():
        m = _TEMP_CMD.match(line)
        if not m:
            continue
        code, temp = m.group(1).upper(), float(m.group(2))
        key = "hotend" if code in ("M104", "M109") else "bed" if code in ("M140", "M190") else "chamber"
        peaks[key] = max(peaks[key], temp)
    return peaks


def build_server(cfg: dict[str, Any] | None = None, *, backend: PrinterBackend | None = None,
                 guard: Guard | None = None) -> MCPServer:
    cfg = cfg or {}
    pcfg = cfg.get("printer", {})
    limits = PrinterLimits(**pcfg.get("limits", {}))
    if backend is None:
        backend = make_backend(
            pcfg.get("backend", "octoprint"),
            pcfg.get("url", "http://127.0.0.1:5000"),
            api_key=os.environ.get(pcfg.get("api_key_env", "AEGIS_PRINTER_API_KEY")),
            webcam_snapshot_url=pcfg.get("webcam_snapshot_url"),
        )
    guard = guard or make_guard(cfg, "printer")
    mcp = MCPServer(
        "aegis-printer",
        instructions=(
            "Controls one 3D printer. Read status before acting. Temperatures are in Celsius. "
            "High-risk actions return status=confirmation_required: ask the human for the code, never invent it."
        ),
    )

    def g(tool: str, args: dict[str, Any], action: Any, risk: Risk, summary: str, **kw: Any) -> Any:
        return guard.run(tool, args, action, risk=risk, summary=summary, **kw)

    # ------------------------------------------------------------------ read tools
    @mcp.tool(**tool_kwargs(Risk.READ, title="Printer status", idempotent=True))
    async def printer_status() -> dict:
        """Printer state, heater temperatures and current job in one call."""
        return await g("printer_status", {}, backend.status, Risk.READ, "read status")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Temperatures", idempotent=True))
    async def printer_get_temperatures() -> dict:
        """Actual and target temperature of every heater (hotends, bed, chamber)."""
        async def act() -> dict:
            return (await backend.status())["temperatures"]
        return await g("printer_get_temperatures", {}, act, Risk.READ, "read temperatures")

    @mcp.tool(**tool_kwargs(Risk.READ, title="Print progress", idempotent=True))
    async def printer_progress() -> dict:
        """Progress of the current print: file, percent complete, elapsed and estimated remaining seconds."""
        async def act() -> dict:
            s = await backend.status()
            return {"state": s["state"], **s["job"]}
        return await g("printer_progress", {}, act, Risk.READ, "read progress")

    @mcp.tool(**tool_kwargs(Risk.READ, title="List G-code files", idempotent=True))
    async def printer_list_files() -> dict:
        """List G-code files stored on the printer host."""
        return await g("printer_list_files", {}, backend.list_files, Risk.READ, "list files")

    # ------------------------------------------------------------------ actuation
    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Set heater temperature"))
    async def printer_set_temperature(
        heater: Annotated[Heater, Field(description="tool0/tool1 = hotends, bed, chamber")],
        target_c: Annotated[float, Field(ge=0, le=400, description="Target in Celsius; 0 turns the heater off")],
        confirm_token: Annotated[str | None, Field(description="Only when re-calling after confirmation_required")] = None,
    ) -> dict:
        """Set a heater target. 0 = off (always allowed). Above the configured threshold needs human confirmation;
        above the hard maximum is refused."""
        args = {"heater": heater, "target_c": target_c}
        risk = Risk.LOW if target_c == 0 else Risk.HIGH if target_c > limits.confirm_above(heater) else Risk.MEDIUM

        def validate() -> None:
            if target_c > limits.hard_max(heater):
                raise SafetyViolation(f"{heater} target {target_c} C exceeds hard limit {limits.hard_max(heater)} C")

        async def act() -> dict:
            await backend.set_temperature(heater, target_c)
            return {"heater": heater, "target_c": target_c}

        return await g("printer_set_temperature", args, act, risk, f"set {heater} to {target_c:g} C",
                       validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Upload G-code"))
    async def printer_upload_gcode(
        filename: Annotated[str, Field(description="e.g. benchy.gcode (letters, digits, . _ - space)")],
        gcode: Annotated[str, Field(description="Full G-code text")],
        confirm_token: str | None = None,
    ) -> dict:
        """Upload G-code to the printer. The file is scanned: temperatures above hard limits are refused, above the
        confirmation thresholds the upload needs human confirmation."""
        data = gcode.encode("utf-8")
        peaks = scan_gcode(gcode)
        args = {"filename": filename, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "peaks": peaks}
        hot = peaks["hotend"] > limits.confirm_hotend_above_c or peaks["bed"] > limits.confirm_bed_above_c
        risk = Risk.HIGH if hot else Risk.MEDIUM

        def validate() -> None:
            if not _FILENAME.match(filename) or "/" in filename or "\\" in filename:
                raise SafetyViolation("invalid filename; use a plain name ending in .gcode")
            if len(data) > limits.max_upload_bytes:
                raise SafetyViolation(f"file is {len(data)} bytes, limit {limits.max_upload_bytes}")
            if peaks["hotend"] > limits.max_hotend_c:
                raise SafetyViolation(f"G-code heats hotend to {peaks['hotend']} C > limit {limits.max_hotend_c} C")
            if peaks["bed"] > limits.max_bed_c:
                raise SafetyViolation(f"G-code heats bed to {peaks['bed']} C > limit {limits.max_bed_c} C")
            if peaks["chamber"] > limits.max_chamber_c:
                raise SafetyViolation(f"G-code heats chamber to {peaks['chamber']} C > limit {limits.max_chamber_c} C")

        async def act() -> dict:
            return {**(await backend.upload(filename, data)), "peak_temps_c": peaks}

        return await g("printer_upload_gcode", args, act, risk, f"upload {filename} ({len(data)} bytes)",
                       validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.HIGH, title="Start print"))
    async def printer_start_print(filename: str, confirm_token: str | None = None) -> dict:
        """Start printing a stored file. Unattended heating + motion, so this needs human confirmation by default."""
        async def act() -> dict:
            st = await backend.status()
            if st["printing"]:
                raise SafetyViolation("printer is already printing")
            await backend.start(filename)
            return {"started": filename}

        return await g("printer_start_print", {"filename": filename}, act, Risk.HIGH, f"start print {filename}",
                       confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.LOW, title="Pause print"))
    async def printer_pause() -> dict:
        """Pause the current print (safe direction)."""
        return await g("printer_pause", {}, backend.pause, Risk.LOW, "pause print")

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Resume print"))
    async def printer_resume() -> dict:
        """Resume a paused print."""
        return await g("printer_resume", {}, backend.resume, Risk.MEDIUM, "resume print")

    @mcp.tool(**tool_kwargs(Risk.MEDIUM, title="Cancel print"))
    async def printer_cancel() -> dict:
        """Cancel the current print (the job is lost, heaters are left to the printer's end G-code)."""
        return await g("printer_cancel", {}, backend.cancel, Risk.MEDIUM, "cancel print")

    # ------------------------------------------------------------------ resources
    @mcp.resource("printer://status", mime_type="application/json", description="Live printer status")
    async def status_resource() -> str:
        try:
            return json.dumps(await backend.status())
        except PrinterError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource("printer://webcam/snapshot", mime_type="image/jpeg", description="Current webcam frame")
    async def snapshot_resource() -> bytes:
        try:
            return await backend.snapshot()
        except PrinterError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource("printer://files", mime_type="application/json", description="Stored G-code files")
    async def files_resource() -> str:
        try:
            return json.dumps(await backend.list_files())
        except PrinterError as exc:
            raise ResourceError(str(exc)) from exc

    mcp.aegis_guard = guard  # type: ignore[attr-defined]  # exposed for tests / embedding
    return mcp


def _build_from_cli(cfg: dict[str, Any], args: argparse.Namespace) -> MCPServer:
    p = cfg.setdefault("printer", {})
    if args.backend:
        p["backend"] = args.backend
    if args.url:
        p["url"] = args.url
    guard = make_guard(cfg, "printer", overrides={"dry_run": True} if args.dry_run else None)
    return build_server(cfg, guard=guard)


def main(argv: Sequence[str] | None = None) -> None:
    def extra(p: argparse.ArgumentParser) -> None:
        p.add_argument("--backend", choices=["octoprint", "moonraker"])
        p.add_argument("--url", help="printer host base URL, e.g. http://octopi.local")

    cli_main(_build_from_cli, "aegis-mcp-printer", "MCP server for OctoPrint / Moonraker printers", argv, extra)


if __name__ == "__main__":
    main()
