"""Multi-server MCP agent loop with routed models and client-side safety enforcement.

The orchestrator is an MCP *client*: it connects to any number of MCP servers (stdio
subprocesses, streamable-HTTP URLs, or in-process ``MCPServer`` objects in tests), merges
their tools under ``<server>__<tool>`` names, and runs a plan -> act -> observe loop where
the :class:`~aegis_core.router.Router` picks the model for every step.

Safety is enforced twice (defense in depth):

* client side - deny lists, a cap on how risky a call may be without a human
  (``autonomous_max_risk``), a per-run tool-call budget, and an audit trail;
* server side - each aegis-mcp server's own Guard. When a server answers
  ``confirmation_required`` the orchestrator asks the human (``approver``) for the code
  and retries; it never fabricates one.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Awaitable, Callable, Literal

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel, Field, field_validator

from aegis_mcp.safety import RISK_META_KEY, AuditLog, Risk

from .router import RouteRequest, Router
from .types import Message, ToolCall, ToolSpec

SEP = "__"

DEFAULT_SYSTEM = (
    "You are AEGIS, an assistant that operates real hardware (3D printer, drone, NAS, lab bench) through tools. "
    "Before acting, state a short plan. Read state before changing it. Prefer the least risky tool that achieves the goal. "
    "If a tool returns status=confirmation_required, do not retry with a guessed confirm_token: the human must supply it. "
    "If a tool returns blocked, explain why to the user instead of working around it. Be concise."
)


class ServerSpec(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    url: str | None = None

    def target(self) -> Any:
        if self.url:
            return self.url
        if self.command:
            return StdioServerParameters(command=self.command, args=self.args, env=self.env)
        raise ValueError(f"server {self.name}: set either command or url")


class OrchestratorPolicy(BaseModel):
    autonomous_max_risk: Risk = Risk.MEDIUM
    deny_tools: list[str] = Field(default_factory=list, description="globs on qualified names, e.g. 'drone__*'")
    max_steps: int = 12
    max_tool_calls: int = 30
    max_result_chars: int = 12_000
    plan_first: bool = False

    @field_validator("autonomous_max_risk", mode="before")
    @classmethod
    def _r(cls, v: Any) -> Risk:
        return Risk.parse(v)


@dataclass
class ToolEntry:
    server: str
    name: str
    qualified: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk

    @property
    def server_confirms(self) -> bool:
        return "confirm_token" in (self.input_schema.get("properties") or {})


@dataclass
class ApprovalRequest:
    kind: Literal["pre_call", "server_confirmation"]
    tool: str
    arguments: dict[str, Any]
    risk: str
    summary: str = ""
    request_id: str | None = None


Approver = Callable[[ApprovalRequest], Awaitable["bool | str | None"]]


@dataclass
class StepRecord:
    tier: str
    model: str
    difficulty: float
    cost_usd: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    route_reasons: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    text: str
    steps: list[StepRecord]
    cost_usd: float
    messages: list[Message]
    stopped_reason: str


def tool_risk(tool: Any) -> Risk:
    meta = getattr(tool, "meta", None) or {}
    if RISK_META_KEY in meta:
        try:
            return Risk.parse(meta[RISK_META_KEY])
        except ValueError:
            pass
    ann = getattr(tool, "annotations", None)
    if ann is not None:
        if getattr(ann, "read_only_hint", None):
            return Risk.READ
        if getattr(ann, "destructive_hint", None):
            return Risk.HIGH
    return Risk.MEDIUM  # unknown third-party tools are treated as actuating


class Orchestrator:
    def __init__(self, router: Router, servers: dict[str, Any], *, policy: OrchestratorPolicy | None = None,
                 approver: Approver | None = None, system_prompt: str | None = None,
                 audit: AuditLog | None = None) -> None:
        self.router = router
        self.server_targets = servers
        self.policy = policy or OrchestratorPolicy()
        self.approver = approver
        self.system_prompt = system_prompt or DEFAULT_SYSTEM
        self.audit = audit or AuditLog(None, server="orchestrator")
        self.clients: dict[str, Client] = {}
        self.tools: dict[str, ToolEntry] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "Orchestrator":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for name, target in self.server_targets.items():
                client = Client(target)
                await self._stack.enter_async_context(client)
                self.clients[name] = client
                await self._load_tools(name, client)
        except BaseException:
            await self._stack.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.__aexit__(*exc)

    async def _load_tools(self, server: str, client: Client) -> None:
        cursor = None
        while True:
            res = await client.list_tools(cursor=cursor)
            for t in res.tools:
                q = f"{server}{SEP}{t.name}"
                self.tools[q] = ToolEntry(server, t.name, q, t.description or "", t.input_schema, tool_risk(t))
            cursor = res.next_cursor
            if not cursor:
                break

    def tool_specs(self) -> list[ToolSpec]:
        out = []
        for e in self.tools.values():
            if any(fnmatch(e.qualified, p) for p in self.policy.deny_tools):
                continue
            out.append(ToolSpec(e.qualified, f"[{e.server} | risk:{e.risk.label}] {e.description}", e.input_schema))
        return out

    # ------------------------------------------------------------------ tool execution
    async def _raw_call(self, entry: ToolEntry, args: dict[str, Any]) -> tuple[str, bool]:
        res = await self.clients[entry.server].call_tool(entry.name, args)
        parts = []
        for c in res.content:
            if getattr(c, "type", None) == "text":
                parts.append(c.text)
            else:
                parts.append(f"<{getattr(c, 'type', 'content')} omitted>")
        return "".join(parts), bool(res.is_error)

    async def execute(self, call: ToolCall) -> tuple[str, bool, dict[str, Any]]:
        """Run one tool call through client-side policy. Returns (text, is_error, record)."""
        rec: dict[str, Any] = {"tool": call.name, "arguments": call.arguments}
        entry = self.tools.get(call.name)
        if entry is None:
            rec["outcome"] = "unknown_tool"
            return f"unknown tool {call.name}", True, rec
        if any(fnmatch(call.name, p) for p in self.policy.deny_tools):
            rec["outcome"] = "denied_by_policy"
            self.audit.record(tool=call.name, outcome="denied_by_policy", risk=entry.risk.label, args=call.arguments)
            return json.dumps({"status": "blocked", "reason": "tool denied by orchestrator policy"}), True, rec
        args = {k: v for k, v in call.arguments.items() if k != "confirm_token"}  # model may not self-confirm
        if entry.risk > self.policy.autonomous_max_risk and not entry.server_confirms:
            ok = await self._ask(ApprovalRequest("pre_call", call.name, args, entry.risk.label,
                                                 summary=f"{call.name}({json.dumps(args)})"))
            if ok is not True:
                rec["outcome"] = "declined_by_human"
                self.audit.record(tool=call.name, outcome="declined_by_human", risk=entry.risk.label, args=args)
                return json.dumps({"status": "blocked", "reason": "the human operator declined this action"}), True, rec
        text, is_error = await self._raw_call(entry, args)
        body = _try_json(text)
        if isinstance(body, dict) and body.get("status") == "confirmation_required":
            answer = await self._ask(ApprovalRequest("server_confirmation", call.name, args, body.get("risk", "?"),
                                                     summary=body.get("summary", ""), request_id=body.get("request_id")))
            if isinstance(answer, str) and answer:
                text, is_error = await self._raw_call(entry, {**args, "confirm_token": answer})
                body = _try_json(text)
            else:
                rec["outcome"] = "confirmation_declined"
                self.audit.record(tool=call.name, outcome="confirmation_declined", risk=entry.risk.label, args=args)
                return json.dumps({"status": "blocked", "reason": "the human operator did not confirm"}), True, rec
        rec["outcome"] = body.get("status", "error" if is_error else "ok") if isinstance(body, dict) else (
            "error" if is_error else "ok")
        self.audit.record(tool=call.name, outcome=rec["outcome"], risk=entry.risk.label, args=args)
        if len(text) > self.policy.max_result_chars:
            text = text[: self.policy.max_result_chars] + f"... [truncated {len(text) - self.policy.max_result_chars} chars]"
        return text, is_error, rec

    async def _ask(self, req: ApprovalRequest) -> bool | str | None:
        if self.approver is None:
            return None
        return await self.approver(req)

    # ------------------------------------------------------------------ agent loop
    async def run(self, prompt: str, *, history: list[Message] | None = None, latency_budget_s: float | None = None,
                  cost_budget_usd: float | None = None) -> RunResult:
        messages: list[Message] = list(history or []) + [Message("user", prompt)]
        steps: list[StepRecord] = []
        total = 0.0
        calls_made = 0
        specs = self.tool_specs()

        async def step(tools: list[ToolSpec]) -> Any:
            nonlocal total
            rreq = RouteRequest(messages=messages, system=self.system_prompt, tools=tools,
                                latency_budget_s=latency_budget_s, cost_budget_usd=cost_budget_usd)
            routed = await self.router.complete(rreq)
            total += routed.cost_usd
            r = routed.response
            messages.append(Message("assistant", r.text, tool_calls=r.tool_calls, raw=r.raw, raw_provider=r.provider,
                                    raw_model=r.model))
            steps.append(StepRecord(routed.tier.name, routed.tier.model, routed.decision.difficulty, routed.cost_usd,
                                    route_reasons=routed.decision.reasons))
            return r

        if self.policy.plan_first:
            messages[-1] = Message("user", prompt + "\n\nFirst reply with a short numbered plan only; do not call tools yet.")
            await step([])
            messages.append(Message("user", "Execute the plan now using the tools. Stop and report when done."))

        for _ in range(self.policy.max_steps):
            r = await step(specs)
            if not r.tool_calls:
                return RunResult(r.text, steps, round(total, 8), messages, "final_answer")
            for call in r.tool_calls:
                if calls_made >= self.policy.max_tool_calls:
                    text, is_error, rec = json.dumps({"status": "blocked", "reason": "tool-call budget for this run exhausted"}), True, {
                        "tool": call.name, "outcome": "budget_exhausted"}
                else:
                    calls_made += 1
                    text, is_error, rec = await self.execute(call)
                steps[-1].tool_calls.append(rec)
                messages.append(Message("tool", text, tool_call_id=call.id, name=call.name, is_error=is_error))
        last = next((m.content for m in reversed(messages) if m.role == "assistant" and m.content), "")
        return RunResult(last, steps, round(total, 8), messages, "max_steps")


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
