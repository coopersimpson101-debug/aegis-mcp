"""Show which tier/model the router picks for a few prompts (no API calls are made).

    python examples/route_explain.py
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from aegis_core import Message, RouteRequest, Router, parse_config
from aegis_core.providers import ScriptedProvider

raw = tomllib.loads((Path(__file__).resolve().parents[1] / "config" / "aegis.example.toml").read_text())
raw["router"]["budget_file"] = None
cfg = parse_config(raw, providers={"anthropic": ScriptedProvider([]), "ollama": ScriptedProvider([])})
router = Router(cfg.router, cfg.providers)

PROMPTS = [
    ("what's the bed temperature?", {}),
    ("turn off the soldering iron", {}),
    ("Compare PETG and ABS for an enclosure part and explain how bed temperature affects warping.", {}),
    ("Plan a survey flight, debug why the EKF altitude drifts, analyze the logs and design a calibration strategy.", {}),
    ("Plan a survey flight, debug why the EKF altitude drifts, analyze the logs and design a calibration strategy.",
     {"latency_budget_s": 5}),
]
for text, kw in PROMPTS:
    d = router.choose(RouteRequest(messages=[Message("user", text)], **kw))
    print(f"{d.tier.name:9} {d.tier.model:20} score={d.difficulty:.2f} est=${d.est_cost_usd:.5f}  {text[:60]!r} {kw or ''}")
    print(f"{'':9} reasons: {'; '.join(d.reasons)}")
