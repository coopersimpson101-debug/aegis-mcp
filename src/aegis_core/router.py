"""Cost-aware model router.

Each request is scored for *difficulty* (0..1) by a transparent heuristic, mapped to the
cheapest tier whose ``max_difficulty`` covers it, then constrained by context size, latency
budget, per-request cost cap and the remaining daily budget. On failure it falls back
(escalating first, then trying cheaper tiers); on a low-confidence answer it escalates.
Prices and latencies come from config - they are inputs you maintain, not truths in code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator

from .budget import BudgetExceeded, BudgetTracker
from .types import ChatRequest, ChatResponse, Message, Provider, ProviderError, ToolSpec


class Tier(BaseModel):
    name: str
    provider: str
    model: str
    max_difficulty: float = Field(ge=0, le=1)
    input_usd_per_mtok: float = Field(default=0.0, ge=0)
    output_usd_per_mtok: float = Field(default=0.0, ge=0)
    est_latency_s: float = Field(default=10.0, gt=0)
    context_tokens: int = 200_000
    max_output_tokens: int = 8192
    options: dict[str, Any] = Field(default_factory=dict)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok) / 1_000_000


class RouterConfig(BaseModel):
    tiers: list[Tier]
    daily_budget_usd: float = 5.0
    per_request_budget_usd: float | None = 0.50
    chars_per_token: float = 4.0
    expected_output_tokens: int = 800
    low_confidence_threshold: float = 0.5
    max_attempts: int = 3
    budget_file: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "RouterConfig":
        if not self.tiers:
            raise ValueError("router needs at least one tier")
        ds = [t.max_difficulty for t in self.tiers]
        if ds != sorted(ds):
            raise ValueError("tiers must be ordered weakest -> strongest (ascending max_difficulty)")
        return self


@dataclass
class RouteRequest:
    messages: list[Message]
    system: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    latency_budget_s: float | None = None
    cost_budget_usd: float | None = None
    min_tier: str | None = None
    expected_output_tokens: int | None = None


@dataclass
class RouteDecision:
    tier: Tier
    index: int
    difficulty: float
    features: dict[str, float]
    est_input_tokens: int
    est_cost_usd: float
    reasons: list[str]


@dataclass
class Attempt:
    tier: str
    model: str
    outcome: str  # ok | error | low_confidence | skipped_budget
    detail: str = ""
    cost_usd: float = 0.0


@dataclass
class RoutedResponse:
    response: ChatResponse
    tier: Tier
    decision: RouteDecision
    attempts: list[Attempt]
    cost_usd: float


# ----------------------------------------------------------------------------- difficulty
COMPLEX = ("plan", "design", "debug", "diagnose", "analy", "why", "prove", "optimi", "refactor", "compare",
           "strategy", "trade-off", "tradeoff", "step by step", "multi-step", "investigate", "root cause",
           "architecture", "calibrat", "write a", "explain how")
SIMPLE = ("status", "what is the", "turn on", "turn off", "temperature", "list ", "how much", "is the", "show me",
          "battery", "progress")


class DifficultyEstimator:
    """Heuristic difficulty score. Replace with your own (e.g. a small classifier) via ``Router(estimator=...)``."""

    def __call__(self, req: RouteRequest, input_tokens: int) -> tuple[float, dict[str, float]]:
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "").lower()
        f: dict[str, float] = {"base": 0.2}
        f["length"] = min(0.3, input_tokens / 20_000 * 0.3)
        hits = sum(1 for k in COMPLEX if k in last_user)
        f["complex_terms"] = min(0.6, 0.12 * hits)
        f["simple_terms"] = -min(0.15, 0.075 * sum(1 for k in SIMPLE if k in last_user))
        f["code"] = 0.1 if "```" in last_user or re.search(r"\bdef |\bclass |;\s*$", last_user, re.M) else 0.0
        f["tool_rounds"] = min(0.15, 0.03 * sum(1 for m in req.messages if m.role == "tool"))
        f["many_tools"] = 0.05 if len(req.tools) > 20 else 0.0
        score = max(0.0, min(1.0, sum(f.values())))
        return round(score, 4), {k: round(v, 4) for k, v in f.items()}


HEDGES = ("i'm not sure", "i am not sure", "i don't know", "i cannot determine", "unclear to me", "i'm unable to")


def heuristic_confidence(resp: ChatResponse) -> float:
    if resp.confidence is not None:
        return resp.confidence
    if resp.stop_reason in ("max_tokens", "length"):
        return 0.3
    if not resp.text.strip() and not resp.tool_calls:
        return 0.0
    if any(h in resp.text.lower() for h in HEDGES):
        return 0.4
    return 1.0


# ----------------------------------------------------------------------------- router
class Router:
    def __init__(self, config: RouterConfig, providers: dict[str, Provider], *, budget: BudgetTracker | None = None,
                 estimator: Callable[[RouteRequest, int], tuple[float, dict[str, float]]] | None = None,
                 confidence: Callable[[ChatResponse], float] = heuristic_confidence) -> None:
        self.config = config
        self.providers = providers
        self.budget = budget or BudgetTracker(config.daily_budget_usd, config.budget_file)
        self.estimator = estimator or DifficultyEstimator()
        self.confidence = confidence
        missing = {t.provider for t in config.tiers} - set(providers)
        if missing:
            raise ValueError(f"tiers reference unconfigured providers: {sorted(missing)}")

    def estimate_tokens(self, req: RouteRequest) -> int:
        chars = len(req.system or "")
        for m in req.messages:
            chars += len(m.content) + sum(len(json.dumps(tc.arguments)) + len(tc.name) for tc in m.tool_calls)
        chars += sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) for t in req.tools)
        return int(chars / self.config.chars_per_token) + 8 * len(req.messages)

    def _cap(self, req: RouteRequest) -> float:
        caps = [self.budget.remaining_today()]
        if self.config.per_request_budget_usd is not None:
            caps.append(self.config.per_request_budget_usd)
        if req.cost_budget_usd is not None:
            caps.append(req.cost_budget_usd)
        return min(caps)

    def choose(self, req: RouteRequest) -> RouteDecision:
        tiers = self.config.tiers
        tokens = self.estimate_tokens(req)
        out_tokens = req.expected_output_tokens or self.config.expected_output_tokens
        score, features = self.estimator(req, tokens)
        reasons: list[str] = []
        idx = next((i for i, t in enumerate(tiers) if score <= t.max_difficulty), len(tiers) - 1)
        reasons.append(f"difficulty {score:.2f} -> {tiers[idx].name}")
        if req.min_tier:
            names = [t.name for t in tiers]
            if req.min_tier not in names:
                raise ValueError(f"unknown tier {req.min_tier!r}")
            if names.index(req.min_tier) > idx:
                idx = names.index(req.min_tier)
                reasons.append(f"min_tier forces {req.min_tier}")
        while idx < len(tiers) - 1 and tiers[idx].context_tokens < tokens + out_tokens:
            idx += 1
            reasons.append(f"context {tokens} tokens needs {tiers[idx].name}")
        if req.latency_budget_s is not None and tiers[idx].est_latency_s > req.latency_budget_s:
            ok = [i for i in range(idx, -1, -1) if tiers[i].est_latency_s <= req.latency_budget_s]
            new = ok[0] if ok else min(range(len(tiers)), key=lambda i: tiers[i].est_latency_s)
            reasons.append(f"latency budget {req.latency_budget_s}s -> {tiers[new].name}")
            idx = new
        cap = self._cap(req)
        cost = tiers[idx].cost(tokens, out_tokens)
        if cost > cap:
            cheaper = [i for i in range(idx - 1, -1, -1) if tiers[i].cost(tokens, out_tokens) <= cap
                       and tiers[i].context_tokens >= tokens + out_tokens]
            if not cheaper:
                raise BudgetExceeded(f"estimated ${cost:.4f} exceeds cap ${cap:.4f} and no cheaper tier fits "
                                     f"(spent today ${self.budget.spent_today():.4f} of ${self.config.daily_budget_usd})")
            reasons.append(f"cost ${cost:.4f} > cap ${cap:.4f} -> {tiers[cheaper[0]].name}")
            idx = cheaper[0]
            cost = tiers[idx].cost(tokens, out_tokens)
        return RouteDecision(tiers[idx], idx, score, features, tokens, round(cost, 6), reasons)

    def _next_index(self, current: int, tried: set[int], *, escalate_only: bool) -> int | None:
        n = len(self.config.tiers)
        for i in range(current + 1, n):
            if i not in tried:
                return i
        if escalate_only:
            return None
        for i in range(current - 1, -1, -1):
            if i not in tried:
                return i
        return None

    async def complete(self, req: RouteRequest, *, max_tokens: int | None = None,
                       decision: RouteDecision | None = None) -> RoutedResponse:
        decision = decision or self.choose(req)
        tiers = self.config.tiers
        attempts: list[Attempt] = []
        tried: set[int] = set()
        total = 0.0
        idx: int | None = decision.index
        best: tuple[ChatResponse, Tier] | None = None
        out_est = req.expected_output_tokens or self.config.expected_output_tokens
        while idx is not None and len(attempts) < self.config.max_attempts:
            tier = tiers[idx]
            tried.add(idx)
            if tier.cost(decision.est_input_tokens, out_est) > self._cap(req) and idx != decision.index:
                attempts.append(Attempt(tier.name, tier.model, "skipped_budget"))
                idx = self._next_index(idx, tried, escalate_only=False)
                continue
            chat = ChatRequest(model=tier.model, messages=req.messages, system=req.system, tools=req.tools,
                               max_tokens=min(max_tokens or tier.max_output_tokens, tier.max_output_tokens),
                               options=dict(tier.options))
            try:
                resp = await self.providers[tier.provider].complete(chat)
            except ProviderError as exc:
                attempts.append(Attempt(tier.name, tier.model, "error", str(exc)))
                idx = self._next_index(idx, tried, escalate_only=False) if exc.retryable else None
                continue
            cost = tier.cost(resp.usage.input_tokens, resp.usage.output_tokens)
            self.budget.record(cost)
            total += cost
            conf = self.confidence(resp)
            best = (resp, tier)
            if conf < self.config.low_confidence_threshold:
                nxt = self._next_index(idx, tried, escalate_only=True)
                if nxt is not None and len(attempts) + 1 < self.config.max_attempts:
                    attempts.append(Attempt(tier.name, tier.model, "low_confidence", f"confidence {conf:.2f}", cost))
                    idx = nxt
                    continue
            attempts.append(Attempt(tier.name, tier.model, "ok", f"confidence {conf:.2f}", cost))
            return RoutedResponse(resp, tier, decision, attempts, round(total, 8))
        if best is not None:  # every escalation failed; return the best low-confidence answer we have
            return RoutedResponse(best[0], best[1], decision, attempts, round(total, 8))
        raise ProviderError("all tiers failed: " + "; ".join(f"{a.tier}: {a.outcome} {a.detail}" for a in attempts),
                            retryable=False)


def with_messages(req: RouteRequest, messages: list[Message]) -> RouteRequest:
    return replace(req, messages=messages)
