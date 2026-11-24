"""Rate limiting, geofencing and path-traversal protection."""

from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Callable

from pydantic import BaseModel, Field, model_validator


class SafetyViolation(Exception):
    """Raised when a call is refused by a safety rule (not a backend failure)."""


# --------------------------------------------------------------------------- rate limit
@dataclass
class _Bucket:
    capacity: float
    refill_per_s: float
    tokens: float
    stamp: float


class RateLimiter:
    """Token bucket per key. ``limits`` maps a tool glob to ``(calls, per_seconds)``."""

    def __init__(self, limits: dict[str, tuple[int, float]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = dict(limits or {})
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _limit_for(self, key: str) -> tuple[int, float] | None:
        if key in self.limits:
            return self.limits[key]
        for pattern, lim in self.limits.items():
            if pattern != "*" and fnmatch(key, pattern):
                return lim
        return self.limits.get("*")

    def has_explicit(self, key: str) -> bool:
        return any(p != "*" and (p == key or fnmatch(key, p)) for p in self.limits)

    def allow(self, key: str, *, use_default: bool = True) -> bool:
        lim = self._limit_for(key)
        if lim is not None and not use_default and not self.has_explicit(key):
            lim = None
        if lim is None:
            return True
        calls, per = lim
        now = self.clock()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = _Bucket(calls, calls / per, float(calls), now)
            b.tokens = min(b.capacity, b.tokens + (now - b.stamp) * b.refill_per_s)
            b.stamp = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True
            return False


# --------------------------------------------------------------------------- geofence
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


class Geofence(BaseModel):
    """Cylinder around home (always) intersected with an optional polygon."""

    home_lat: float = Field(ge=-90, le=90)
    home_lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=100.0, gt=0)
    max_alt_m: float = Field(default=50.0, gt=0, description="Relative to home")
    min_alt_m: float = 0.0
    polygon: list[tuple[float, float]] | None = Field(default=None, description="[(lat, lon), ...] optional")

    @model_validator(mode="after")
    def _check(self) -> "Geofence":
        if self.polygon is not None and len(self.polygon) < 3:
            raise ValueError("geofence polygon needs at least 3 vertices")
        if self.min_alt_m >= self.max_alt_m:
            raise ValueError("min_alt_m must be below max_alt_m")
        return self

    def violations(self, lat: float, lon: float, alt_m: float) -> list[str]:
        out = []
        d = haversine_m(self.home_lat, self.home_lon, lat, lon)
        if d > self.radius_m:
            out.append(f"{d:.1f} m from home exceeds radius {self.radius_m:.1f} m")
        if alt_m > self.max_alt_m:
            out.append(f"altitude {alt_m:.1f} m exceeds max {self.max_alt_m:.1f} m")
        if alt_m < self.min_alt_m:
            out.append(f"altitude {alt_m:.1f} m below min {self.min_alt_m:.1f} m")
        if self.polygon and not point_in_polygon(lat, lon, self.polygon):
            out.append("point is outside the geofence polygon")
        return out

    def check(self, lat: float, lon: float, alt_m: float) -> None:
        v = self.violations(lat, lon, alt_m)
        if v:
            raise SafetyViolation("geofence: " + "; ".join(v))


# --------------------------------------------------------------------------- paths
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass
class PathGuard:
    """Maps *virtual* paths like ``media/movies/a.mkv`` onto configured roots.

    The first path component is the root name. Anything that tries to leave the root
    (``..``, absolute paths, drive letters, UNC paths, NUL bytes, symlinks pointing outside)
    is rejected with :class:`SafetyViolation`.
    """

    roots: dict[str, Path]
    deny_globs: list[str] = field(default_factory=lambda: ["**/.ssh/**", "**/*.key", "**/*.pem", "**/.aegis-state/**"])

    def __post_init__(self) -> None:
        self.roots = {name: Path(p).expanduser().resolve() for name, p in self.roots.items()}
        for name in self.roots:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise ValueError(f"invalid root name {name!r}")

    def resolve(self, vpath: str) -> Path:
        if not isinstance(vpath, str) or not vpath.strip():
            raise SafetyViolation("path must be a non-empty string like '<root>/<relative path>'")
        if "\x00" in vpath:
            raise SafetyViolation("NUL byte in path")
        raw = vpath.replace("\\", "/").strip()
        if raw.startswith("/") or raw.startswith("//") or _WIN_DRIVE.match(raw):
            raise SafetyViolation("absolute paths are not allowed; use '<root>/<relative path>'")
        parts = PurePosixPath(raw).parts
        if any(p == ".." for p in parts):
            raise SafetyViolation("'..' path components are not allowed")
        root_name, rest = parts[0], parts[1:]
        if root_name not in self.roots:
            raise SafetyViolation(f"unknown root {root_name!r}; allowed roots: {sorted(self.roots)}")
        root = self.roots[root_name]
        real = root.joinpath(*rest).resolve() if rest else root
        if real != root and root not in real.parents:
            raise SafetyViolation("path escapes its root (symlink or traversal)")
        rel = real.relative_to(root).as_posix()
        for g in self.deny_globs:
            if fnmatch("/" + rel, g) or fnmatch(rel, g.removeprefix("**/")):
                raise SafetyViolation(f"path matches deny rule {g!r}")
        return real

    def to_virtual(self, real: Path) -> str:
        real = Path(real).resolve()
        for name, root in self.roots.items():
            if real == root:
                return name
            if root in real.parents:
                return f"{name}/{real.relative_to(root).as_posix()}"
        raise SafetyViolation("path is outside every root")

    def is_denied(self, real: Path) -> bool:
        try:
            self.resolve(self.to_virtual(real))
            return False
        except SafetyViolation:
            return True


def env_or_none(name: str | None) -> str | None:
    return os.environ.get(name) if name else None
