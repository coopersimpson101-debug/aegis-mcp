"""Append-only, hash-chained JSONL audit log.

Every line carries ``prev`` (hash of the previous line) and ``hash`` (sha256 over the
canonical JSON of the record without ``hash``). Editing, deleting or reordering lines
breaks the chain, which :func:`verify_audit_log` detects. This is tamper-*evidence*, not
tamper-proofing: ship the log off-box if you need the latter.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
REDACT_KEYS = {"api_key", "apikey", "token", "password", "passwd", "secret", "confirm_token", "authorization"}


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively replace secret-looking keys and truncate huge strings."""
    if _depth > 6:
        return "<nested>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in REDACT_KEYS:
                out[k] = "<redacted>" if v not in (None, "") else v
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 512:
        return value[:512] + f"...<{len(value) - 512} more chars>"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


class AuditLog:
    """Thread-safe append-only JSONL writer. ``path=None`` keeps records in memory only."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, server: str = "aegis") -> None:
        self.path = Path(path).expanduser() if path else None
        self.server = server
        self._lock = threading.Lock()
        self._memory: list[dict[str, Any]] = []
        self._prev = GENESIS
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._prev = self._last_hash()

    def _last_hash(self) -> str:
        assert self.path is not None
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line).get("hash", last)
                    except json.JSONDecodeError:
                        continue
        return last

    def record(self, *, tool: str, outcome: str, risk: str | None = None, args: dict[str, Any] | None = None,
               detail: Any = None) -> dict[str, Any]:
        with self._lock:
            rec: dict[str, Any] = {
                "ts": round(time.time(), 3),
                "server": self.server,
                "tool": tool,
                "risk": risk,
                "outcome": outcome,
                "args": redact(args or {}),
                "detail": redact(detail),
                "prev": self._prev,
            }
            rec["hash"] = hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()
            self._prev = rec["hash"]
            if self.path is not None:
                # Open in append mode for every record: never truncates, survives log rotation.
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(_canonical(rec) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._memory.append(rec)
            return rec

    @property
    def records(self) -> list[dict[str, Any]]:
        """Records written by *this* process (handy for tests)."""
        return list(self._memory)


def verify_audit_log(path: str | os.PathLike[str]) -> tuple[bool, int, str]:
    """Return ``(ok, lines_checked, message)``."""
    prev = GENESIS
    n = 0
    with Path(path).open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, n, f"line {n}: not valid JSON"
            claimed = rec.pop("hash", None)
            if rec.get("prev") != prev:
                return False, n, f"line {n}: chain broken (prev mismatch)"
            actual = hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()
            if actual != claimed:
                return False, n, f"line {n}: hash mismatch (record edited)"
            prev = claimed
    return True, n, "ok"
