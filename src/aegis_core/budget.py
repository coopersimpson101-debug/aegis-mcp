"""Per-day spend tracking (UTC days), optionally persisted to a JSON file."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class BudgetExceeded(Exception):
    pass


class BudgetTracker:
    def __init__(self, daily_limit_usd: float, path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.daily_limit_usd = daily_limit_usd
        self.path = Path(path).expanduser() if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self._days: dict[str, float] = {}
        if self.path and self.path.exists():
            try:
                self._days = {k: float(v) for k, v in json.loads(self.path.read_text("utf-8")).items()}
            except (ValueError, OSError):
                self._days = {}

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        with self._lock:
            return self._days.get(self._today(), 0.0)

    def remaining_today(self) -> float:
        return max(0.0, self.daily_limit_usd - self.spent_today())

    def record(self, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        with self._lock:
            day = self._today()
            self._days[day] = round(self._days.get(day, 0.0) + cost_usd, 8)
            # keep ~2 months of history
            for old in sorted(self._days)[:-62]:
                self._days.pop(old)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._days, indent=1), "utf-8")
                os.replace(tmp, self.path)

    def history(self) -> dict[str, float]:
        with self._lock:
            return dict(self._days)
