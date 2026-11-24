"""Human-in-the-loop confirmation tokens.

When a call needs confirmation the server creates a *pending confirmation* bound to the
exact tool name and a digest of the arguments. The model only receives the ``request_id``.
The human gets the short ``code`` out-of-band (server stderr / log, and a JSON file under
``<state_dir>/pending/``). Two ways to approve:

1. The human tells the assistant the code ("confirm 4F7Q2K"), and the assistant repeats
   the identical call with ``confirm_token="4F7Q2K"``.
2. The human runs ``aegis-mcp approve <request_id>`` on the host, then the assistant
   repeats the call with ``confirm_token=<request_id>``.

Tokens are single-use, expire (default 120 s) and are invalid for any other tool or for
*different arguments* - so "confirm heat bed to 60" cannot be replayed to heat it to 120.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity


def args_digest(tool: str, args: dict[str, Any]) -> str:
    clean = {k: v for k, v in sorted(args.items()) if k != "confirm_token"}
    blob = json.dumps({"tool": tool, "args": clean}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class PendingConfirmation:
    request_id: str
    code: str
    tool: str
    digest: str
    summary: str
    risk: str
    created: float
    expires: float

    def public(self) -> dict[str, Any]:
        """What the model is allowed to see (no code)."""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "risk": self.risk,
            "summary": self.summary,
            "expires_in_s": max(0, int(self.expires - time.time())),
        }


Notifier = Callable[[PendingConfirmation], None]


def stderr_notifier(p: PendingConfirmation) -> None:
    print(
        f"[aegis-safety] CONFIRMATION REQUIRED request_id={p.request_id} code={p.code} "
        f"tool={p.tool} risk={p.risk} :: {p.summary}",
        file=sys.stderr,
        flush=True,
    )


class ConfirmationManager:
    def __init__(self, *, ttl_s: float = 120.0, state_dir: str | os.PathLike[str] | None = None,
                 notifier: Notifier | None = stderr_notifier, clock: Callable[[], float] = time.time) -> None:
        self.ttl_s = ttl_s
        self.state_dir = Path(state_dir).expanduser() if state_dir else None
        self.notifier = notifier
        self.clock = clock
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()
        if self.state_dir:
            (self.state_dir / "pending").mkdir(parents=True, exist_ok=True)
            (self.state_dir / "approved").mkdir(parents=True, exist_ok=True)

    # -- issuing -----------------------------------------------------------------
    def request(self, tool: str, args: dict[str, Any], *, summary: str, risk: str) -> PendingConfirmation:
        now = self.clock()
        p = PendingConfirmation(
            request_id="cr_" + secrets.token_hex(6),
            code="".join(secrets.choice(_ALPHABET) for _ in range(6)),
            tool=tool,
            digest=args_digest(tool, args),
            summary=summary,
            risk=risk,
            created=now,
            expires=now + self.ttl_s,
        )
        with self._lock:
            self._gc(now)
            self._pending[p.request_id] = p
        if self.state_dir:
            (self.state_dir / "pending" / f"{p.request_id}.json").write_text(json.dumps(asdict(p), indent=2), "utf-8")
        if self.notifier:
            self.notifier(p)
        return p

    def _gc(self, now: float) -> None:
        for rid in [rid for rid, p in self._pending.items() if p.expires < now]:
            self._drop(rid)

    def _drop(self, rid: str) -> None:
        self._pending.pop(rid, None)
        if self.state_dir:
            for sub in ("pending", "approved"):
                try:
                    (self.state_dir / sub / f"{rid}.json").unlink()
                except FileNotFoundError:
                    pass

    # -- verifying ---------------------------------------------------------------
    def consume(self, tool: str, args: dict[str, Any], token: str | None) -> bool:
        """Return True (and burn the token) if ``token`` authorises this exact call."""
        if not token:
            return False
        token = token.strip()
        digest = args_digest(tool, args)
        now = self.clock()
        with self._lock:
            self._gc(now)
            candidates = list(self._pending.values())
            for p in candidates:
                if p.tool != tool or p.digest != digest:
                    continue
                by_code = hmac.compare_digest(p.code.upper(), token.upper())
                by_approval = hmac.compare_digest(p.request_id, token) and self._approved_on_host(p.request_id)
                if by_code or by_approval:
                    self._drop(p.request_id)
                    return True
        return False

    def _approved_on_host(self, rid: str) -> bool:
        return bool(self.state_dir and (self.state_dir / "approved" / f"{rid}.json").exists())

    def pending(self) -> list[PendingConfirmation]:
        with self._lock:
            self._gc(self.clock())
            return list(self._pending.values())


def approve_on_host(state_dir: str | os.PathLike[str], request_id: str) -> PendingConfirmation:
    """Used by ``aegis-mcp approve``: mark a pending request as approved by a human on the host."""
    base = Path(state_dir).expanduser()
    src = base / "pending" / f"{request_id}.json"
    if not src.exists():
        raise FileNotFoundError(f"no pending confirmation {request_id!r} in {base}")
    data = json.loads(src.read_text("utf-8"))
    p = PendingConfirmation(**data)
    if p.expires < time.time():
        raise TimeoutError(f"confirmation {request_id} expired")
    (base / "approved").mkdir(parents=True, exist_ok=True)
    (base / "approved" / f"{request_id}.json").write_text(json.dumps({"approved_at": time.time()}), "utf-8")
    return p


def list_pending_on_host(state_dir: str | os.PathLike[str]) -> list[PendingConfirmation]:
    base = Path(state_dir).expanduser() / "pending"
    out = []
    for f in sorted(base.glob("*.json")) if base.exists() else []:
        try:
            p = PendingConfirmation(**json.loads(f.read_text("utf-8")))
        except (ValueError, TypeError):
            continue
        if p.expires >= time.time():
            out.append(p)
    return out
