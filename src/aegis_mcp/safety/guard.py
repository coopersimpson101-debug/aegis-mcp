"""The guard every tool call passes through.

Order of checks (first failure wins, everything is audited):

1. allow/deny lists            -> ``blocked``
2. effective risk > max_risk   -> ``blocked``
3. rate limit                  -> ``rate_limited``
4. domain validators (geofence, temperature caps, path guard) raise SafetyViolation -> ``blocked``
5. dry-run mode                -> ``dry_run`` (nothing executed)
6. risk >= confirm_at and no valid token -> ``confirmation_required``
7. execute                     -> ``ok`` / ``error``
"""

from __future__ import annotations

import inspect
from fnmatch import fnmatch
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, field_validator

from .audit import AuditLog
from .confirm import ConfirmationManager, Notifier, stderr_notifier
from .limits import RateLimiter, SafetyViolation
from .risk import Risk


class SafetyPolicy(BaseModel):
    """The ``[safety]`` table of every server config."""

    dry_run: bool = False
    confirm_at: Risk = Risk.HIGH
    max_risk: Risk = Risk.CRITICAL
    allow_tools: list[str] | None = Field(default=None, description="Glob patterns. None = all tools allowed.")
    deny_tools: list[str] = Field(default_factory=list)
    risk_overrides: dict[str, Risk] = Field(default_factory=dict, description="tool name -> minimum risk")
    rate_limits: dict[str, tuple[int, float]] = Field(
        default_factory=lambda: {"*": (60, 60.0)}, description="tool glob -> (calls, per_seconds)"
    )
    confirmation_ttl_s: float = 120.0
    audit_log: str | None = "~/.aegis/audit.jsonl"
    state_dir: str | None = "~/.aegis/state"

    @field_validator("confirm_at", "max_risk", mode="before")
    @classmethod
    def _parse_risk(cls, v: Any) -> Risk:
        return Risk.parse(v)

    @field_validator("risk_overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, v: Any) -> dict[str, Risk]:
        return {k: Risk.parse(r) for k, r in (v or {}).items()}

    @field_validator("rate_limits", mode="before")
    @classmethod
    def _parse_limits(cls, v: Any) -> dict[str, tuple[int, float]]:
        out = {}
        for k, lim in (v or {}).items():
            if isinstance(lim, dict):
                out[k] = (int(lim["calls"]), float(lim["per_seconds"]))
            else:
                out[k] = (int(lim[0]), float(lim[1]))
        return out


class Guard:
    def __init__(self, policy: SafetyPolicy | None = None, *, server: str = "aegis", audit: AuditLog | None = None,
                 confirmations: ConfirmationManager | None = None, rate_limiter: RateLimiter | None = None,
                 notifier: Notifier | None = stderr_notifier) -> None:
        self.policy = policy or SafetyPolicy()
        self.server = server
        self.audit = audit or AuditLog(self.policy.audit_log, server=server)
        self.confirmations = confirmations or ConfirmationManager(
            ttl_s=self.policy.confirmation_ttl_s, state_dir=self.policy.state_dir, notifier=notifier
        )
        self.rate_limiter = rate_limiter or RateLimiter(self.policy.rate_limits)

    # ------------------------------------------------------------------ helpers
    def is_allowed(self, tool: str) -> bool:
        if any(fnmatch(tool, p) for p in self.policy.deny_tools):
            return False
        if self.policy.allow_tools is None:
            return True
        return any(fnmatch(tool, p) for p in self.policy.allow_tools)

    def effective_risk(self, tool: str, risk: Risk) -> Risk:
        override = self.policy.risk_overrides.get(tool)
        return max(risk, override) if override is not None else risk

    def _blocked(self, tool: str, risk: Risk, args: dict[str, Any], reason: str, status: str = "blocked") -> dict:
        self.audit.record(tool=tool, outcome=status, risk=risk.label, args=args, detail=reason)
        return {"status": status, "tool": tool, "risk": risk.label, "reason": reason}

    # ------------------------------------------------------------------ main entry
    async def run(
        self,
        tool: str,
        args: dict[str, Any],
        action: Callable[[], Any | Awaitable[Any]],
        *,
        risk: Risk,
        summary: str,
        validate: Callable[[], None] | None = None,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        risk = self.effective_risk(tool, Risk.parse(risk))
        if not self.is_allowed(tool):
            return self._blocked(tool, risk, args, "tool is not allowed by this server's safety policy")
        if risk > self.policy.max_risk:
            return self._blocked(tool, risk, args, f"risk {risk.label} exceeds max_risk {self.policy.max_risk.label}")
        # The "*" default limit applies to actuating tools; READ tools only if a rule names them.
        if not self.rate_limiter.allow(tool, use_default=risk > Risk.READ):
            return self._blocked(tool, risk, args, "rate limit exceeded, slow down", status="rate_limited")
        if validate is not None:
            try:
                validate()
            except SafetyViolation as exc:
                return self._blocked(tool, risk, args, str(exc))
        if self.policy.dry_run:
            self.audit.record(tool=tool, outcome="dry_run", risk=risk.label, args=args, detail=summary)
            return {"status": "dry_run", "tool": tool, "risk": risk.label, "would_do": summary}
        if risk >= self.policy.confirm_at and not self.confirmations.consume(tool, args, confirm_token):
            pending = self.confirmations.request(tool, args, summary=summary, risk=risk.label)
            self.audit.record(tool=tool, outcome="confirmation_required", risk=risk.label, args=args,
                              detail={"request_id": pending.request_id, "summary": summary,
                                      "token_supplied": bool(confirm_token)})
            return {
                "status": "confirmation_required",
                **pending.public(),
                "how_to_confirm": (
                    "Ask the human operator to approve. They will see a 6-character code in the server log "
                    "(or run `aegis-mcp approve <request_id>` on the host). Then call this tool again with "
                    "identical arguments plus confirm_token=<code or request_id>. Never guess the code."
                ),
                **({"note": "the supplied confirm_token was invalid, expired, or for different arguments"}
                   if confirm_token else {}),
            }
        try:
            result = action()
            if inspect.isawaitable(result):
                result = await result
        except SafetyViolation as exc:
            return self._blocked(tool, risk, args, str(exc))
        except ToolError:
            self.audit.record(tool=tool, outcome="error", risk=risk.label, args=args, detail="tool error")
            raise
        except Exception as exc:  # backend failure: audit, then surface a clean message to the model
            self.audit.record(tool=tool, outcome="error", risk=risk.label, args=args, detail=f"{type(exc).__name__}: {exc}")
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        self.audit.record(tool=tool, outcome="ok", risk=risk.label, args=args, detail=summary)
        return {"status": "ok", "tool": tool, "risk": risk.label, "result": result}
