"""MCP server for a NAS: filesystem operations confined to allowed roots, SMART health,
optional TrueNAS/Synology appliance API.

It works on any mounted filesystem (local disk, SMB/CIFS mount, NFS mount), so it does not
care which protocol the share uses - run it on the NAS itself or on a box that mounts it.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import shutil
import stat as statmod
import time
from pathlib import Path
from typing import Annotated, Any, Sequence

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from ..common import cli_main, make_guard, tool_kwargs
from ..safety import Guard, PathGuard, Risk, SafetyViolation
from . import smart as smartmod
from .appliance import Appliance, Synology, TrueNAS

TRASH = ".aegis-trash"


class NasSettings(BaseModel):
    roots: dict[str, str] = Field(default_factory=dict)
    writable_roots: list[str] = Field(default_factory=list, description="Roots where delete is permitted")
    deny_globs: list[str] | None = None
    max_read_bytes: int = 1_048_576
    max_list_entries: int = 1000
    smartctl: str = "smartctl"
    smart_devices: list[str] = Field(default_factory=list, description="Optional allowlist of devices")


def _entry(p: Path, guard: PathGuard) -> dict[str, Any]:
    st = p.lstat()
    kind = "dir" if statmod.S_ISDIR(st.st_mode) else "symlink" if statmod.S_ISLNK(st.st_mode) else "file"
    return {"path": guard.to_virtual(p) if kind != "symlink" else None, "name": p.name, "type": kind,
            "size": st.st_size if kind == "file" else None, "modified": round(st.st_mtime, 1)}


def make_appliance(acfg: dict[str, Any] | None) -> Appliance | None:
    if not acfg:
        return None
    kind = acfg.get("kind", "").lower()
    if kind == "truenas":
        return TrueNAS(acfg["url"], os.environ.get(acfg.get("api_key_env", "AEGIS_TRUENAS_API_KEY"), ""),
                       verify=acfg.get("verify_tls", True))
    if kind == "synology":
        return Synology(acfg["url"], os.environ.get(acfg.get("account_env", "AEGIS_SYNOLOGY_USER"), ""),
                        os.environ.get(acfg.get("password_env", "AEGIS_SYNOLOGY_PASSWORD"), ""),
                        verify=acfg.get("verify_tls", True))
    raise ValueError(f"unknown appliance kind {kind!r}")


def build_server(cfg: dict[str, Any] | None = None, *, guard: Guard | None = None,
                 smart_runner: smartmod.Runner | None = None, appliance: Appliance | None = None) -> MCPServer:
    cfg = cfg or {}
    ncfg = cfg.get("nas", {})
    settings = NasSettings(**{k: v for k, v in ncfg.items() if k in NasSettings.model_fields})
    if not settings.roots:
        raise ValueError("configure at least one NAS root, e.g. --root media=/mnt/tank/media")
    pg_kwargs: dict[str, Any] = {"roots": {k: Path(v) for k, v in settings.roots.items()}}
    if settings.deny_globs is not None:
        pg_kwargs["deny_globs"] = settings.deny_globs
    paths = PathGuard(**pg_kwargs)
    paths.deny_globs = [*paths.deny_globs, f"{TRASH}/**", f"**/{TRASH}/**"]
    guard = guard or make_guard(cfg, "nas")
    runner = smart_runner or smartmod.default_runner(settings.smartctl)
    appliance = appliance if appliance is not None else make_appliance(ncfg.get("appliance"))

    mcp = MCPServer("aegis-nas", instructions=(
        "Filesystem access to a NAS. Paths are virtual: '<root>/<relative path>' (see nas_list_roots). "
        "Deleting moves files to a per-root trash unless permanent=true; both need human confirmation."))

    def run(tool: str, args: dict[str, Any], fn: Any, risk: Risk = Risk.READ, summary: str = "", **kw: Any) -> Any:
        return guard.run(tool, args, lambda: anyio.to_thread.run_sync(fn), risk=risk, summary=summary or tool, **kw)

    @mcp.tool(**tool_kwargs(Risk.READ, title="List roots", idempotent=True, open_world=False))
    async def nas_list_roots() -> dict:
        """Names of the shares/roots this server exposes and whether they are writable."""
        def fn() -> list[dict[str, Any]]:
            return [{"root": n, "writable": n in settings.writable_roots} for n in paths.roots]
        return await run("nas_list_roots", {}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="List directory", idempotent=True, open_world=False))
    async def nas_list(
        path: Annotated[str, Field(description="Virtual path '<root>' or '<root>/<dir>'")],
        include_hidden: bool = False,
        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
    ) -> dict:
        """List a directory (dirs first, then files, alphabetical)."""
        def fn() -> dict[str, Any]:
            d = paths.resolve(path)
            if not d.is_dir():
                raise SafetyViolation(f"{path} is not a directory")
            items = []
            for child in sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if child.name == TRASH or (not include_hidden and child.name.startswith(".")) or paths.is_denied(child):
                    continue
                items.append(_entry(child, paths))
                if len(items) >= min(limit, settings.max_list_entries):
                    break
            return {"path": path, "entries": items, "truncated": len(items) >= min(limit, settings.max_list_entries)}
        return await run("nas_list", {"path": path}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="Stat path", idempotent=True, open_world=False))
    async def nas_stat(path: str) -> dict:
        """Size, type and modification time of one path."""
        return await run("nas_stat", {"path": path}, lambda: _entry(paths.resolve(path), paths))

    @mcp.tool(**tool_kwargs(Risk.READ, title="Search files", idempotent=True, open_world=False))
    async def nas_search(
        path: Annotated[str, Field(description="Where to start, e.g. 'media' or 'docs/2024'")],
        pattern: Annotated[str, Field(description="Case-insensitive glob on file names, e.g. '*.pdf' or '*invoice*'")],
        max_results: Annotated[int, Field(ge=1, le=2000)] = 100,
        max_depth: Annotated[int, Field(ge=0, le=32)] = 8,
    ) -> dict:
        """Recursive filename search (does not follow symlinks)."""
        def fn() -> dict[str, Any]:
            start = paths.resolve(path)
            base_depth = len(start.parts)
            hits: list[dict[str, Any]] = []
            pat = pattern.lower()
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dp = Path(dirpath)
                dirnames[:] = [d for d in dirnames if d != TRASH and not paths.is_denied(dp / d)]
                if len(dp.parts) - base_depth >= max_depth:
                    dirnames[:] = []
                for name in [*dirnames, *filenames]:
                    if fnmatch.fnmatch(name.lower(), pat):
                        p = dp / name
                        if p.is_symlink() or paths.is_denied(p):
                            continue
                        hits.append(_entry(p, paths))
                        if len(hits) >= max_results:
                            return {"matches": hits, "truncated": True}
            return {"matches": hits, "truncated": False}
        return await run("nas_search", {"path": path, "pattern": pattern}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="Read file", idempotent=True, open_world=False))
    async def nas_read(
        path: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=16_777_216)] = 65_536,
    ) -> dict:
        """Read part of a file. Text comes back as UTF-8, binary as base64."""
        def fn() -> dict[str, Any]:
            p = paths.resolve(path)
            if not p.is_file():
                raise SafetyViolation(f"{path} is not a regular file")
            n = min(max_bytes, settings.max_read_bytes)
            with p.open("rb") as fh:
                head = fh.read(4096)  # sniff the file start, not just this chunk
                fh.seek(offset)
                data = fh.read(n)
            size = p.stat().st_size
            out: dict[str, Any] = {"path": path, "offset": offset, "bytes": len(data), "file_size": size,
                                   "eof": offset + len(data) >= size}
            try:
                if b"\x00" in head or b"\x00" in data:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
                out["encoding"], out["content"] = "utf-8", data.decode("utf-8")
            except UnicodeDecodeError:
                out["encoding"], out["content"] = "base64", base64.b64encode(data).decode()
            return out
        return await run("nas_read", {"path": path, "offset": offset, "max_bytes": max_bytes}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="Disk usage", idempotent=True, open_world=False))
    async def nas_disk_usage(
        path: Annotated[str, Field(description="Any path on the volume, usually a root name")],
        measure_directory: Annotated[bool, Field(description="Also sum file sizes under path (slow on big trees)")] = False,
    ) -> dict:
        """Free/used/total bytes of the volume holding path, optionally the size of the directory itself."""
        def fn() -> dict[str, Any]:
            p = paths.resolve(path)
            du = shutil.disk_usage(p)
            out: dict[str, Any] = {"path": path, "total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
                                   "used_pct": round(100 * du.used / du.total, 1) if du.total else None}
            if measure_directory:
                total, files, t0 = 0, 0, time.monotonic()
                for dirpath, _dirnames, filenames in os.walk(p, followlinks=False):
                    for f in filenames:
                        try:
                            total += os.lstat(os.path.join(dirpath, f)).st_size
                            files += 1
                        except OSError:
                            pass
                    if time.monotonic() - t0 > 20:
                        out["directory_partial"] = True
                        break
                out["directory_bytes"], out["directory_files"] = total, files
            return out
        return await run("nas_disk_usage", {"path": path, "measure_directory": measure_directory}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="SMART scan", idempotent=True))
    async def nas_smart_scan() -> dict:
        """List disks smartctl can see."""
        def fn() -> list[dict[str, Any]]:
            devs = smartmod.scan(runner)
            if settings.smart_devices:
                devs = [d for d in devs if d["name"] in settings.smart_devices]
            return devs
        return await run("nas_smart_scan", {}, fn)

    @mcp.tool(**tool_kwargs(Risk.READ, title="SMART health", idempotent=True))
    async def nas_smart_status(
        device: Annotated[str | None, Field(description="e.g. /dev/sda; omit for every disk")] = None,
    ) -> dict:
        """SMART health verdict (OK / WARNING / FAILING) plus the counters that predict failure."""
        def fn() -> list[dict[str, Any]]:
            devs = smartmod.scan(runner)
            if settings.smart_devices:
                devs = [d for d in devs if d["name"] in settings.smart_devices]
            if device is not None:
                devs = [d for d in devs if d["name"] == device]
                if not devs:
                    raise SafetyViolation(f"device {device!r} not found by smartctl --scan (or not allowlisted)")
            return [smartmod.device_health(runner, d["name"], d.get("type")) for d in devs]
        return await run("nas_smart_status", {"device": device}, fn)

    @mcp.tool(**tool_kwargs(Risk.HIGH, title="Delete path", open_world=False))
    async def nas_delete(
        path: str,
        permanent: Annotated[bool, Field(description="Skip the trash. Irreversible: CRITICAL risk.")] = False,
        confirm_token: str | None = None,
    ) -> dict:
        """Delete a file or directory. Default: move into '<root>/.aegis-trash/<timestamp>/'. Needs confirmation."""
        root_name = path.replace("\\", "/").split("/", 1)[0]

        def validate() -> None:
            p = paths.resolve(path)
            if p in paths.roots.values():
                raise SafetyViolation("refusing to delete a root")
            if root_name not in settings.writable_roots:
                raise SafetyViolation(f"root {root_name!r} is read-only (writable_roots={settings.writable_roots})")
            if not p.exists() and not p.is_symlink():
                raise SafetyViolation(f"{path} does not exist")

        def fn() -> dict[str, Any]:
            validate()
            p = paths.resolve(path)
            if permanent:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                return {"deleted": path, "permanent": True}
            trash = paths.roots[root_name] / TRASH / time.strftime("%Y%m%d-%H%M%S")
            rel = p.relative_to(paths.roots[root_name])
            dest = trash / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            return {"deleted": path, "permanent": False, "trash": f"{root_name}/{TRASH}/{trash.name}/{rel.as_posix()}"}

        risk = Risk.CRITICAL if permanent else Risk.HIGH
        return await run("nas_delete", {"path": path, "permanent": permanent}, fn, risk,
                         ("PERMANENTLY delete " if permanent else "move to trash: ") + path,
                         validate=validate, confirm_token=confirm_token)

    @mcp.tool(**tool_kwargs(Risk.READ, title="Appliance health", idempotent=True))
    async def nas_appliance_health() -> dict:
        """Pool/volume health from the TrueNAS or Synology API, if configured."""
        async def fn() -> Any:
            if appliance is None:
                raise SafetyViolation("no appliance API configured ([nas.appliance])")
            return await appliance.health()
        return await guard.run("nas_appliance_health", {}, fn, risk=Risk.READ, summary="appliance health")

    @mcp.resource("nas://roots", mime_type="application/json", description="Exposed roots")
    def roots_resource() -> str:
        return json.dumps([{"root": n, "writable": n in settings.writable_roots} for n in paths.roots])

    mcp.aegis_guard = guard  # type: ignore[attr-defined]
    return mcp


def _build_from_cli(cfg: dict[str, Any], args: argparse.Namespace) -> MCPServer:
    n = cfg.setdefault("nas", {})
    roots = n.setdefault("roots", {})
    for spec in args.root or []:
        name, _, p = spec.partition("=")
        roots[name] = p
    if args.writable:
        n["writable_roots"] = list({*n.get("writable_roots", []), *args.writable})
    guard = make_guard(cfg, "nas", overrides={"dry_run": True} if args.dry_run else None)
    return build_server(cfg, guard=guard)


def main(argv: Sequence[str] | None = None) -> None:
    def extra(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", action="append", help="NAME=PATH, repeatable")
        p.add_argument("--writable", action="append", help="root name where deletes are allowed")

    cli_main(_build_from_cli, "aegis-mcp-nas", "MCP server for NAS filesystems + SMART", argv, extra)


if __name__ == "__main__":
    main()
