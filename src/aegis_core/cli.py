"""``aegis-orchestrate``: run one prompt through the router + MCP servers, or explain a routing decision."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Sequence

from aegis_mcp.safety import AuditLog

from .config import load_config
from .orchestrator import ApprovalRequest, Orchestrator
from .router import RouteRequest, Router
from .types import Message


async def console_approver(req: ApprovalRequest) -> bool | str | None:
    """Ask on the terminal. For server confirmations the human types the code shown in the server log."""
    loop = asyncio.get_running_loop()
    if req.kind == "pre_call":
        ans = await loop.run_in_executor(None, input, f"\n[approve?] {req.risk.upper()} {req.summary}  [y/N] ")
        return ans.strip().lower() in ("y", "yes")
    ans = await loop.run_in_executor(
        None, input, f"\n[confirm] {req.tool} ({req.risk}): {req.summary}\n"
                     f"  enter the code from the server log (or the request id after `aegis-mcp approve {req.request_id}`),"
                     f" empty to decline: ")
    return ans.strip() or None


async def _run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = Router(cfg.router, cfg.providers)
    if args.explain:
        d = router.choose(RouteRequest(messages=[Message("user", args.prompt)]))
        print(json.dumps({"tier": d.tier.name, "model": d.tier.model, "difficulty": d.difficulty,
                          "features": d.features, "est_input_tokens": d.est_input_tokens,
                          "est_cost_usd": d.est_cost_usd, "reasons": d.reasons,
                          "spent_today_usd": router.budget.spent_today()}, indent=2))
        return 0
    servers = {s.name: s.target() for s in cfg.servers}
    async with Orchestrator(router, servers, policy=cfg.policy, approver=console_approver,
                            system_prompt=cfg.system_prompt, audit=AuditLog(cfg.audit_log, server="orchestrator")) as orch:
        if args.list_tools:
            for e in orch.tools.values():
                print(f"{e.risk.label:8} {e.qualified}")
            return 0
        res = await orch.run(args.prompt, latency_budget_s=args.latency_budget, cost_budget_usd=args.cost_budget)
    for s in res.steps:
        print(f"  step: {s.tier}/{s.model} difficulty={s.difficulty:.2f} cost=${s.cost_usd:.5f} "
              f"tools={[c['tool'] + ':' + str(c.get('outcome')) for c in s.tool_calls]}", file=sys.stderr)
    print(res.text)
    print(f"[total ${res.cost_usd:.5f}, today ${router.budget.spent_today():.4f}]", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aegis-orchestrate", description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("prompt", nargs="?", default="")
    p.add_argument("--explain", action="store_true", help="only show which tier/model the router would pick")
    p.add_argument("--list-tools", action="store_true")
    p.add_argument("--latency-budget", type=float)
    p.add_argument("--cost-budget", type=float)
    sys.exit(asyncio.run(_run(p.parse_args(argv))))


if __name__ == "__main__":
    main()
