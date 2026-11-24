"""``aegis-mcp`` operator CLI: approve confirmations, verify the audit log, run simulators."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

from .safety import approve_on_host, list_pending_on_host, verify_audit_log

DEFAULT_STATE = "~/.aegis/state"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aegis-mcp", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pending", help="list pending confirmation requests")
    s.add_argument("--state-dir", default=DEFAULT_STATE)

    s = sub.add_parser("approve", help="approve a pending request (then the assistant retries with the request_id)")
    s.add_argument("request_id")
    s.add_argument("--state-dir", default=DEFAULT_STATE)

    s = sub.add_parser("audit", help="verify the hash chain of an audit log")
    s.add_argument("path", nargs="?", default="~/.aegis/audit.jsonl")

    s = sub.add_parser("sim", help="run a simulator for local demos")
    s.add_argument("device", choices=["octoprint", "moonraker", "vehicle"])
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--api-key", default="sim-key", help="API key the fake OctoPrint expects")

    a = p.parse_args(argv)
    if a.cmd == "pending":
        items = list_pending_on_host(a.state_dir)
        if not items:
            print("no pending confirmations")
        for it in items:
            print(f"{it.request_id}  {it.risk:8}  {it.tool:28}  expires in {int(it.expires - time.time()):4d}s  {it.summary}")
        return 0
    if a.cmd == "approve":
        try:
            it = approve_on_host(a.state_dir, a.request_id)
        except (FileNotFoundError, TimeoutError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"approved {it.request_id}: {it.tool} ({it.risk}) {it.summary}")
        return 0
    if a.cmd == "audit":
        from pathlib import Path

        ok, n, msg = verify_audit_log(Path(a.path).expanduser())
        print(f"{'OK' if ok else 'TAMPERED'}: {n} lines checked - {msg}")
        return 0 if ok else 2
    if a.cmd == "sim":
        return _sim(a)
    return 1  # pragma: no cover


def _sim(a: argparse.Namespace) -> int:
    if a.device == "vehicle":
        from .sim.vehicle import FakeMavlinkVehicle

        # home = ArduPilot SITL's default (CMAC), so config/drone.example.toml's geofence fits
        v = FakeMavlinkVehicle(a.port or 5760, home_lat=-35.363262, home_lon=149.165237).start()
        print(f"fake ArduCopter listening; connect with --connection {v.connection_string}", flush=True)
    else:
        from .sim.printers import FakeMoonraker, FakeOctoPrint

        app = FakeOctoPrint(api_key=a.api_key) if a.device == "octoprint" else FakeMoonraker()
        srv, _ = app.serve(port=a.port or (5000 if a.device == "octoprint" else 7125))
        print(f"fake {a.device} on http://127.0.0.1:{srv.server_address[1]}"
              + (f" (X-Api-Key: {a.api_key})" if a.device == "octoprint" else ""), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
