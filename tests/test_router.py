from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from aegis_core import BudgetExceeded, BudgetTracker, Message, ProviderError, RouteRequest, Router, parse_config
from aegis_core.providers import ScriptedProvider, say

RAW = tomllib.loads((Path(__file__).parents[1] / "config" / "aegis.example.toml").read_text())


def router(steps_anthropic=(), steps_ollama=(), *, budget=None, **router_over):
    raw = {**RAW, "router": {**RAW["router"], "budget_file": None, **router_over}}
    provs = {"anthropic": ScriptedProvider(list(steps_anthropic), "anthropic"),
             "ollama": ScriptedProvider(list(steps_ollama), "ollama")}
    cfg = parse_config(raw, providers=provs)
    return Router(cfg.router, provs, budget=budget), provs


def rq(text: str, **kw) -> RouteRequest:
    return RouteRequest(messages=[Message("user", text)], **kw)


def test_default_model_ids_come_from_config():
    r, _ = router()
    assert [t.model for t in r.config.tiers[1:]] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


def test_simple_goes_local_hard_goes_opus():
    r, _ = router()
    assert r.choose(rq("what is the bed temperature?")).tier.name == "local"
    d = r.choose(rq("Plan and debug why the drone's altitude estimate drifts; analyze the logs, compare EKF "
                    "settings and design a calibration strategy step by step."))
    assert d.tier.name == "deep" and d.difficulty > 0.7 and d.reasons
    mid = r.choose(rq("Compare the two filament profiles and explain how retraction affects stringing."))
    assert mid.tier.name in ("fast", "balanced")


def test_long_context_moves_off_small_local_model():
    r, _ = router()
    r.estimator = lambda req, tokens: (0.0, {"forced": 0.0})  # pretend it's trivial; only context matters
    assert r.choose(rq("status")).tier.name == "local"
    d = r.choose(rq("status " + "x" * 200_000))
    assert d.tier.name != "local" and any("context" in x for x in d.reasons)


def test_latency_budget_downgrades():
    r, _ = router()
    hard = "plan debug analyze design strategy root cause architecture"
    assert r.choose(rq(hard)).tier.name == "deep"
    d = r.choose(rq(hard, latency_budget_s=5))
    assert d.tier.name == "fast" and any("latency" in x for x in d.reasons)


def test_cost_caps_downgrade_or_refuse():
    r, _ = router()
    hard = "plan debug analyze design strategy root cause architecture " + "y" * 40_000
    d = r.choose(rq(hard, cost_budget_usd=0.01))
    assert d.tier.name == "fast" and d.est_cost_usd <= 0.01
    clock_budget = BudgetTracker(1.0)
    clock_budget.record(1.0)  # daily budget exhausted -> only the free local tier fits
    r2, _ = router(budget=clock_budget)
    assert r2.choose(rq("plan debug analyze")).tier.name == "local"
    with pytest.raises(BudgetExceeded):
        r2.choose(rq("status " + "z" * 200_000))  # needs a big-context paid tier, no money left


async def test_fallback_on_provider_error():
    r, provs = router(steps_anthropic=[ProviderError("529 overloaded"), say("from sonnet", usage=(1000, 100))])
    res = await r.complete(rq("Compare the two filament profiles and explain how retraction affects stringing."))
    assert [a.outcome for a in res.attempts] == ["error", "ok"]
    assert res.response.text == "from sonnet" and res.tier.name in ("balanced", "deep")
    assert res.cost_usd == pytest.approx(res.tier.cost(1000, 100))


async def test_non_retryable_error_stops():
    r, _ = router(steps_ollama=[ProviderError("bad request", retryable=False)])
    with pytest.raises(ProviderError, match="all tiers failed"):
        await r.complete(rq("status?"))


async def test_escalates_on_low_confidence_and_tracks_budget(tmp_path):
    budget = BudgetTracker(5.0, tmp_path / "budget.json")
    r, provs = router(steps_ollama=[say("I'm not sure, maybe 60?")],
                      steps_anthropic=[say("Bed is at 60 C.", usage=(2000, 50))], budget=budget)
    res = await r.complete(rq("what is the bed temperature?"))
    assert [a.outcome for a in res.attempts] == ["low_confidence", "ok"]
    assert res.tier.name == "fast" and res.response.text == "Bed is at 60 C."
    assert provs["anthropic"].requests[0].model == "claude-haiku-5-5"
    spent = budget.spent_today()
    assert spent == pytest.approx(2000 * 0.10 / 1e6 + 50 * 0.50 / 1e6)
    assert BudgetTracker(5.0, tmp_path / "budget.json").spent_today() == pytest.approx(spent)  # persisted


async def test_low_confidence_at_top_tier_returns_best_effort():
    r, _ = router(steps_anthropic=[say("unclear", confidence=0.1)])
    res = await r.complete(rq("plan debug analyze design strategy root cause architecture"))
    assert res.tier.name == "deep" and res.attempts[-1].outcome == "ok"


def test_effort_option_passes_through():
    r, _ = router()
    assert r.config.tiers[-1].options == {"effort": "high"}


def test_tier_order_validated():
    from aegis_core import RouterConfig
    with pytest.raises(ValueError):
        RouterConfig(tiers=[{"name": "a", "provider": "x", "model": "m", "max_difficulty": 0.9},
                            {"name": "b", "provider": "x", "model": "m", "max_difficulty": 0.1}])
