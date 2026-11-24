"""Bits shared by all four servers: config loading, tool metadata, CLI runner."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .safety import RISK_META_KEY, Guard, Risk, SafetyPolicy


def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).expanduser().open("rb") as fh:
        return tomllib.load(fh)


def tool_kwargs(risk: Risk, *, title: str | None = None, idempotent: bool | None = None,
                open_world: bool = True) -> dict[str, Any]:
    """Keyword args for ``@server.tool(...)`` advertising the base risk to clients.

    ``_meta['aegis/risk']`` is the machine-readable level the orchestrator enforces; the
    standard MCP annotations give generic clients (Claude Desktop/Code) the same hint.
    """
    return {
        "title": title,
        "annotations": ToolAnnotations(
            title=title,
            read_only_hint=risk == Risk.READ,
            destructive_hint=risk >= Risk.HIGH,
            idempotent_hint=idempotent,
            open_world_hint=open_world,
        ),
        "meta": {RISK_META_KEY: risk.label},
    }


def make_guard(cfg: dict[str, Any], server: str, *, overrides: dict[str, Any] | None = None) -> Guard:
    data = dict(cfg.get("safety", {}))
    data.update(overrides or {})
    if os.environ.get("AEGIS_DRY_RUN", "").lower() in {"1", "true", "yes"}:
        data["dry_run"] = True
    return Guard(SafetyPolicy(**data), server=server)


def base_parser(prog: str, description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("--config", help="TOML config file (see config/*.example.toml)")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="bind address for streamable-http (default loopback)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--dry-run", action="store_true", help="never actuate hardware; report what would happen")
    return p


def run_server(server: MCPServer, args: argparse.Namespace) -> None:
    if args.transport == "stdio":
        server.run("stdio")
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"[aegis] WARNING: binding {args.host}; put an authenticating reverse proxy in front.",
                  file=sys.stderr)
        server.run("streamable-http", host=args.host, port=args.port)


def short(text: str, n: int = 160) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def cli_main(build: Any, prog: str, description: str, argv: Sequence[str] | None = None,
             extra: Any = None) -> None:
    parser = base_parser(prog, description)
    if extra:
        extra(parser)
    args = parser.parse_args(argv)
    cfg = load_toml(args.config)
    server = build(cfg, args)
    run_server(server, args)
