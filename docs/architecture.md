# Architecture

```
                         +--------------------------------------------------------------+
  Claude Desktop /       |  aegis_core (library)                                        |
  Claude Code     ---+   |                                                              |
  (MCP client)       |   |   Orchestrator ---- Router ----+-- AnthropicProvider (SDK)   |
                     |   |   - MCP client per server      +-- OpenAICompatProvider      |
  your AEGIS app ----+-->|   - tool aggregation           +-- OllamaProvider            |
                     |   |   - client-side policy         +-- your Provider             |
                     |   |   - approver (human)          BudgetTracker (per UTC day)    |
                     |   +---------------+----------------------------------------------+
                     |                   | MCP (stdio / streamable HTTP)
                     v                   v
   +-----------------+---+ +-------------+-------+ +------------------+ +--------------------+
   | aegis-mcp-printer   | | aegis-mcp-drone     | | aegis-mcp-nas    | | aegis-mcp-lab      |
   |  OctoPrint backend  | |  MAVLink backend    | |  PathGuard fs ops| |  MQTT (paho)       |
   |  Moonraker backend  | |  AEGIS-Link backend | |  smartctl --json | |  Home Assistant    |
   +----------+----------+ +----------+----------+ |  TrueNAS/Synology| |  scenes, auto-off  |
              |                       |            +--------+---------+ +---------+----------+
              |      every tool call -> Guard.run(): allow/deny, max risk, rate limit,
              |      validators, dry-run, confirmation token, audit (hash-chained JSONL)
              v                       v                     v                     v
        printer host            FC / SITL / radio      mounted shares        broker / HA
```

## Packages

| Path | What |
|------|------|
| `src/aegis_mcp/safety/` | `Risk`, `SafetyPolicy`, `Guard`, `ConfirmationManager`, `RateLimiter`, `Geofence`, `PathGuard`, `AuditLog` |
| `src/aegis_mcp/printer/` | `backends.py` (OctoPrint, Moonraker), `server.py` (tools, G-code scanner, limits) |
| `src/aegis_mcp/drone/` | `base.py` interface, `mavlink.py` (pymavlink), `aegislink.py` (custom serial), `server.py` |
| `src/aegis_mcp/nas/` | `server.py` (fs tools), `smart.py` (smartctl JSON), `appliance.py` (TrueNAS, Synology) |
| `src/aegis_mcp/lab/` | `transports.py` (paho MQTT, HA REST), `server.py` (devices, scenes) |
| `src/aegis_mcp/sim/` | fakes for every device (see `simulators.md`) |
| `src/aegis_mcp/cli.py` | `aegis-mcp pending / approve / audit / sim` |
| `src/aegis_core/` | `types.py`, `router.py`, `budget.py`, `providers/`, `orchestrator.py`, `config.py`, `cli.py` |

## Design choices

* **Official MCP SDK (v2 `MCPServer`, the successor of FastMCP)**. Each server is a
  `build_server(cfg, backend=..., guard=...) -> MCPServer` factory, so tests inject fakes and
  embedders can run servers in-process (`mcp.Client(server)`).
* **Uniform tool results**: `{"status": "ok"|"blocked"|"dry_run"|"confirmation_required"|"rate_limited", ...}`.
  Safety refusals are *results* the model can reason about, not protocol errors. Backend failures
  (printer offline, bad API key) are MCP tool errors with a clean message.
* **Risk is data**: base risk in `_meta["aegis/risk"]` for clients, per-call risk computed from arguments
  on the server. The orchestrator enforces its own ceiling as well.
* **Sync hardware I/O in threads**: pymavlink and filesystem calls run via `anyio.to_thread` so the MCP
  event loop never blocks.
* **Prices and model IDs are config** (`config/aegis.example.toml`), never constants in code.
* **Provider-neutral messages** in `aegis_core.types`; Anthropic native blocks are kept in `Message.raw`
  and replayed unchanged so thinking blocks stay valid across turns.

## Router algorithm

1. Estimate input tokens (chars / `chars_per_token` + tool schemas).
2. Difficulty score 0..1 from `DifficultyEstimator` (length, complex/simple keywords, code, tool rounds).
3. First tier with `max_difficulty >= score` (or `min_tier`).
4. Bump up if the context does not fit; drop to a faster tier if `est_latency_s > latency_budget_s`.
5. Cap = min(per-request budget, remaining daily budget, caller's cost budget). Drop to cheaper tiers
   that fit; if none, `BudgetExceeded`.
6. Call. On `ProviderError`: escalate to untried stronger tiers, then cheaper ones. On low confidence
   (provider confidence, `max_tokens` stop, empty answer, hedging): escalate. Actual cost from usage is
   recorded in the `BudgetTracker`.

## Orchestrator loop

```
messages = history + [user]
(optional) plan step without tools
repeat up to max_steps:
    routed = router.complete(messages, tools)
    if no tool calls: return text
    for each call:
        deny list? -> blocked
        strip model-supplied confirm_token
        risk > autonomous_max_risk and server has no confirmation of its own -> approver(pre_call)
        call tool over MCP
        status == confirmation_required -> approver(server_confirmation) -> retry with code
        append tool result (truncated to max_result_chars)
```
