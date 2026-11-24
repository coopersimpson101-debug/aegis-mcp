# Safety model

aegis-mcp lets a language model touch things that can burn, fall or delete. The safety layer
(`aegis_mcp.safety`) is shared by every server and assumes the model is **helpful but fallible
and possibly prompt-injected** (a G-code comment, a filename or a web page can contain
instructions). It does not assume the host machine is hostile.

## Layers

Every tool call passes through `Guard.run()` in this order. The first failure wins and is audited.

| # | Check | Result | Configured by |
|---|-------|--------|---------------|
| 1 | Tool allow/deny globs | `blocked` | `[safety] allow_tools`, `deny_tools` |
| 2 | Effective risk > `max_risk` | `blocked` | `max_risk`, `risk_overrides` |
| 3 | Token-bucket rate limit (actuating tools by default; READ tools only if named) | `rate_limited` | `[safety.rate_limits]` |
| 4 | Domain validators: geofence, temperature caps, G-code scan, path guard, mode allowlist, HA allowlists | `blocked` | per-server config |
| 5 | Dry-run | `dry_run` (nothing executed) | `dry_run = true`, `--dry-run`, `AEGIS_DRY_RUN=1` |
| 6 | Risk >= `confirm_at` without a valid token | `confirmation_required` | `confirm_at` (default `high`) |
| 7 | Execute | `ok` or tool error | |

## Risk levels

| Level | Meaning | Examples |
|-------|---------|----------|
| `read` | observe only | status, telemetry, list, SMART |
| `low` | safe direction | heater off, pause, land, RTL, outlet off |
| `medium` | normal actuation | bed to 60 C, goto inside fence, lamp on |
| `high` | can damage hardware/property/data | arm motors, takeoff, bed > 90 C, start print, delete to trash, soldering iron on |
| `critical` | irreversible or dangerous to people | force-disarm in flight, permanent delete |

Risk is computed **per call** from the arguments (setting a heater to 0 is `low`, to 100 C is
`high`). Each tool also advertises its base risk in MCP `_meta["aegis/risk"]` and the standard
`readOnlyHint` / `destructiveHint` annotations so any client can show it.

## Human-in-the-loop confirmations

1. The model calls `drone_arm`. The guard creates a pending confirmation bound to the tool name
   and a SHA-256 of the canonical arguments, valid for `confirmation_ttl_s` (120 s).
2. The model receives only `request_id`, a summary and instructions. **It never sees the code.**
3. The 6-character code goes to the human out-of-band: the server's stderr (Claude Desktop/Code
   show it in the MCP log) and `<state_dir>/pending/<id>.json`.
4. The human either tells the assistant the code, or runs `aegis-mcp approve <request_id>` on
   the host and lets the assistant retry with the request id.
5. The retry must have **identical arguments**. Tokens are single-use. A token for
   "heat bed to 100" cannot authorise "heat bed to 110" or a different file.

The orchestrator (`aegis_core`) strips any `confirm_token` the model invents, and routes the
confirmation to its `approver` callback (a terminal prompt in `aegis-orchestrate`).

Limits worth knowing:

* If a human pastes the code into a chat, the model sees it for that one call. That is the
  intended consent path.
* Anyone who can read `<state_dir>` or the server's stderr can approve. Keep `state_dir`
  outside every NAS root (the default deny globs also block `.aegis-state`).

## Domain guards

* **Printer**: hard maxima per heater (refused), confirmation thresholds, G-code upload scanner
  (finds `M104/M109/M140/M190/M141/M191` setpoints above limits), filename allowlist, upload size cap,
  "already printing" check before start.
* **Drone**: geofence is mandatory for takeoff and goto (cylinder around home plus optional polygon,
  altitude band); pre-arm checks for battery, satellites and fence; mode allowlist; force-disarm is
  `critical`; land and RTL are always `low` so a model can reach them. Telemetry reports
  `geofence_ok` and violations.
* **NAS**: virtual paths `<root>/<rel>`; rejects `..`, absolute paths, drive letters, UNC paths, NUL bytes,
  symlinks that resolve outside the root and deny globs (`.ssh`, `*.key`, `*.pem`, the trash).
  Deletes only on `writable_roots`, default to a per-root `.aegis-trash/<timestamp>/`, roots can never be deleted.
  `smartctl` is called with an argument list (no shell) and device names are validated.
* **Lab**: per-device `risk_on`/`risk_off`, optional `auto_off_s` dead-man timer for MQTT outlets,
  scene risk = highest action risk, Home Assistant entity and service allowlists, entity domain must
  match service domain.

## Audit log

Append-only JSONL, one record per decision (`ok`, `blocked`, `dry_run`, `confirmation_required`,
`rate_limited`, `error`). Each record has `prev` and `hash` (SHA-256 chain), so edits, deletions and
reordering are detected by `aegis-mcp audit <path>`. Secrets in arguments (`api_key`, `token`,
`password`, `confirm_token`, ...) are redacted and long strings truncated. This is tamper-evident,
not tamper-proof: ship logs to another machine if that matters to you.

## What this does not protect against

* A compromised host, or a human who approves something they should not.
* Firmware/flight-controller bugs. Keep the vehicle's own failsafes (battery, GCS loss, fence) enabled:
  the MCP geofence is an *extra* fence, not a replacement for ArduPilot's `FENCE_*` parameters.
* Physical-world state the server cannot see (a person under the drone, a spool tangle).
* Network exposure: streamable HTTP binds to 127.0.0.1 by default and has no auth of its own.
  Put an authenticating reverse proxy (or a VPN like Tailscale/WireGuard) in front before exposing it.
