# aegis-mcp

**Let an AI assistant run your 3D printer, drone, NAS and lab bench without letting it burn, crash or delete anything.**

aegis-mcp is a set of [Model Context Protocol](https://modelcontextprotocol.io) servers for physical hardware, plus
`aegis_core`: the cost-aware model router and multi-server orchestrator from AEGIS, a personal JARVIS-style
assistant, released as a library. The servers work with any MCP client (Claude Desktop, Claude Code, your own
agent). Every one of them shares a single safety layer:

- **Per-call risk levels.** A request to heat the bed to 60 C is `medium`. A request for 100 C is `high`, and a
  request for 130 C is refused.
- **Human confirmation codes** for dangerous actions. The model never sees the code, and the code is valid only for
  that exact call, once, for 2 minutes.
- **Geofence.** Takeoff and goto are refused outside it.
- **Path-traversal-proof NAS roots.**
- **Allowlists, rate limits and dry-run mode.**
- **A hash-chained, append-only audit log.**

| Server | Talks to | Highlights |
|---|---|---|
| `aegis-mcp-printer` | OctoPrint REST, Moonraker/Klipper | status, temps, set temps, upload G-code (scanned), start/pause/resume/cancel, progress, webcam snapshot resource |
| `aegis-mcp-drone` | MAVLink via pymavlink (ArduPilot, PX4, SITL), or **AEGIS-Link** serial for custom FC firmware | telemetry, arm/disarm, takeoff/land/RTL, goto with geofence, mode allowlist, pre-arm checks |
| `aegis-mcp-nas` | any mounted filesystem (SMB/NFS/local), `smartctl`, optional TrueNAS/Synology API | list/search/read/stat, disk usage, SMART verdicts, delete-to-trash |
| `aegis-mcp-lab` | MQTT (Tasmota/ESPHome/Shelly style), Home Assistant REST | sensors, relays, power-strip outlets, scenes, dead-man auto-off |
| `aegis_core` | Anthropic (official SDK), OpenAI-compatible endpoints, Ollama | difficulty/latency/cost routing, daily budget, fallback + escalation, agent loop over many MCP servers |

> **Status: alpha, simulator-tested only.** Every test runs against fakes and simulators that live in this repo.
> Nothing here has been validated against a real printer, drone, NAS or lab yet. See
> [Hardware validation](#hardware-validation--todo-for-the-author).

---

## Architecture

```mermaid
flowchart LR
  subgraph clients[MCP clients]
    CD[Claude Desktop / Code]
    AE[your AEGIS app]
  end
  subgraph core[aegis_core library]
    OR[Orchestrator<br/>tool aggregation, policy, approver]
    RT[Router<br/>difficulty, latency, cost, budget]
    PV[Providers<br/>Anthropic, OpenAI-compat, Ollama]
    OR --> RT --> PV
  end
  AE --> OR
  subgraph servers[aegis-mcp servers]
    P[printer] --- G((Guard))
    D[drone] --- G
    N[nas] --- G
    L[lab] --- G
  end
  CD -- stdio / HTTP --> servers
  OR -- stdio / HTTP --> servers
  P --> OP[OctoPrint / Moonraker]
  D --> FC[ArduPilot / PX4 / SITL / custom FC]
  N --> FS[mounted shares + smartctl]
  L --> MQ[MQTT broker / Home Assistant]
  G -.-> AU[(audit.jsonl<br/>hash chain)]
```

The same picture in ASCII, with package paths and design notes, is in [docs/architecture.md](docs/architecture.md).

## Quickstart

```bash
git clone https://github.com/coopersimpson101-debug/aegis-mcp && cd aegis-mcp
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[all]"                              # or ".[dev]" to run the tests
pytest                                               # ~80 tests, all against simulators
python examples/offline_demo.py                      # full orchestrator run, no hardware, no API key
python examples/route_explain.py                     # see which model each prompt would be routed to
```

### Try a server against a simulator

```bash
aegis-mcp sim octoprint --port 5000                  # fake OctoPrint (expects X-Api-Key: sim-key)
AEGIS_PRINTER_API_KEY=sim-key aegis-mcp-printer --url http://127.0.0.1:5000 --transport streamable-http --port 8001

aegis-mcp sim vehicle --port 5760                    # fake ArduCopter speaking MAVLink over TCP
aegis-mcp-drone --config config/drone.example.toml   # same home as ArduPilot SITL, so the example fence fits

aegis-mcp-lab --config config/lab.example.toml --demo   # in-memory broker with simulated plugs
aegis-mcp-nas --root docs=$HOME/Documents            # read-only unless you add --writable docs
```

### Claude Desktop

Add the servers to `claude_desktop_config.json` (full example: [examples/claude_desktop_config.json](examples/claude_desktop_config.json)):

```json
{
  "mcpServers": {
    "aegis-printer": {
      "command": "aegis-mcp-printer",
      "args": ["--config", "/path/to/config/printer.toml"],
      "env": { "AEGIS_PRINTER_API_KEY": "<your key>" }
    },
    "aegis-nas": {
      "command": "aegis-mcp-nas",
      "args": ["--root", "media=/mnt/tank/media", "--root", "docs=/mnt/tank/docs"]
    },
    "aegis-drone": {
      "command": "aegis-mcp-drone",
      "args": ["--config", "/path/to/config/drone.toml", "--dry-run"]
    }
  }
}
```

If `aegis-mcp-*` is not on Claude Desktop's PATH, use the absolute path to the venv's script (or
`"command": "/path/to/.venv/bin/python", "args": ["-m", "aegis_mcp.printer", ...]`). Confirmation codes appear
in the MCP server log (Settings → Developer → open logs) and under `~/.aegis/state/pending/`.

### Claude Code

```bash
claude mcp add aegis-nas -- aegis-mcp-nas --root docs=/mnt/tank/docs
claude mcp add aegis-printer -e AEGIS_PRINTER_API_KEY=<key> -- aegis-mcp-printer --config config/printer.toml
# a server running elsewhere over streamable HTTP:
claude mcp add --transport http aegis-lab http://127.0.0.1:8004/mcp
```

A project-scoped `.mcp.json` example is in [examples/claude_code_mcp.json](examples/claude_code_mcp.json).

### Using the core library (router + orchestrator)

```python
from aegis_core import Orchestrator, Router, load_config

cfg = load_config("config/aegis.example.toml")          # tiers, prices, providers, servers, policy
router = Router(cfg.router, cfg.providers)

async def approver(req):                                 # your UI: terminal, phone push, voice...
    if req.kind == "pre_call":
        return input(f"allow {req.summary}? [y/N] ") == "y"
    return input(f"code for {req.tool} from the server log: ") or None

async with Orchestrator(router, {s.name: s.target() for s in cfg.servers},
                        policy=cfg.policy, approver=approver) as orch:
    result = await orch.run("Heat the bed for PETG and tell me when the lab is above 25 C")
    print(result.text, result.cost_usd, [s.tier for s in result.steps])
```

Or from the shell: `aegis-orchestrate --config config/aegis.example.toml "status of the printer"` and
`aegis-orchestrate --config ... --explain "plan a survey flight"`.

---

## Tool reference

Every tool returns `{"status": ..., "tool", "risk", ...}`. `status` is one of `ok` (with `result`), `blocked` (with
`reason`), `dry_run` (with `would_do`), `confirmation_required` (with `request_id`, `summary`, `expires_in_s`) or
`rate_limited`. Tools marked †  take an optional `confirm_token`.

### Printer (`aegis-mcp-printer`)

| Tool | Risk | Description |
|---|---|---|
| `printer_status` | read | state, all heater temps, current job |
| `printer_get_temperatures` | read | actual/target per heater |
| `printer_progress` | read | file, % complete, elapsed, remaining |
| `printer_list_files` | read | stored G-code files |
| `printer_set_temperature` † | low (0 = off) / medium / **high** above threshold; refused above hard max | `heater`: tool0, tool1, bed, chamber; `target_c` |
| `printer_upload_gcode` † | medium / **high** if the file heats above thresholds; refused above hard max | `filename`, `gcode`. Scans M104/M109/M140/M190/M141/M191 |
| `printer_start_print` † | **high** | `filename`; refused while printing |
| `printer_pause` | low | |
| `printer_resume` | medium | |
| `printer_cancel` | medium | |

Resources: `printer://status` (JSON), `printer://webcam/snapshot` (image/jpeg), `printer://files`.

### Drone (`aegis-mcp-drone`)

| Tool | Risk | Description |
|---|---|---|
| `drone_telemetry` | read | lat/lon/alt, mode, armed, in_air, battery, GPS, heartbeat age, `geofence_ok` |
| `drone_geofence` | read | configured fence |
| `drone_arm` † | **high** | pre-arm checks: battery %, satellites, inside fence |
| `drone_disarm` † | low; `force=true` is **critical** | vehicle refuses a normal disarm in flight |
| `drone_set_mode` | medium (LAND/RTL low) | allowlisted modes only |
| `drone_takeoff` † | **high** | `altitude_m` ≤ min(max_takeoff_alt_m, fence ceiling) |
| `drone_goto` † | medium | `lat`, `lon`, `altitude_m`. Refused outside the fence or with no fence configured |
| `drone_land` | low | always reachable |
| `drone_rtl` | low | always reachable |

Resources: `drone://telemetry`, `drone://geofence`. Backends: `mavlink` (any pymavlink connection string) or
`aegis-link` ([protocol spec](docs/aegis-link-protocol.md)).

### NAS (`aegis-mcp-nas`)

Paths are virtual: `<root>/<relative path>`, where roots are configured names such as `media` or `docs`.

| Tool | Risk | Description |
|---|---|---|
| `nas_list_roots` | read | roots and whether each is writable |
| `nas_list` | read | directory listing (`include_hidden`, `limit`) |
| `nas_stat` | read | type, size, mtime |
| `nas_search` | read | case-insensitive glob on names, `max_results`, `max_depth`, no symlink following |
| `nas_read` | read | `offset`, `max_bytes`; UTF-8 text or base64 |
| `nas_disk_usage` | read | volume total/used/free, optional directory size |
| `nas_smart_scan` | read | disks visible to `smartctl --scan` |
| `nas_smart_status` | read | OK / WARNING / FAILING + reallocated/pending/uncorrectable, NVMe wear and media errors, temperature |
| `nas_delete` † | **high** (to trash); `permanent=true` is **critical** | writable roots only, never a root |
| `nas_appliance_health` | read | TrueNAS pools + alerts, or Synology volumes + disks |

Resource: `nas://roots`.

### Lab (`aegis-mcp-lab`)

| Tool | Risk | Description |
|---|---|---|
| `lab_list_devices` | read | sensors, relays, outlets with their configured risk |
| `lab_read_sensor` / `lab_read_all_sensors` | read | MQTT (plain or JSON payloads) or HA sensors |
| `lab_get_state` | read | relay/outlet on/off from its state topic or HA |
| `lab_set_power` † | per device (`risk_on`, `risk_off`) | waits for the state topic to confirm, arms `auto_off_s` |
| `lab_list_scenes` | read | |
| `lab_activate_scene` † | highest risk of its actions | runs actions in order |
| `lab_ha_state` | read | allowlisted entities only |
| `lab_ha_call_service` † | medium | allowlisted `domain.service` on allowlisted entities |

Resources: `lab://devices`, `lab://sensors`.

### Operator CLI (`aegis-mcp`)

| Command | |
|---|---|
| `aegis-mcp pending [--state-dir]` | list pending confirmations |
| `aegis-mcp approve <request_id>` | approve on the host; the assistant then retries with `confirm_token=<request_id>` |
| `aegis-mcp audit [path]` | verify the audit log hash chain (exit 2 if tampered) |
| `aegis-mcp sim octoprint\|moonraker\|vehicle` | run a simulator |

---

## Safety model (summary)

Every call passes through the guard in this order: allow/deny → `max_risk` → rate limit → domain validators
(geofence, temperature caps, G-code scan, path guard, allowlists) → dry-run → confirmation → execute → audit.
The full write-up, including what this layer does **not** protect against, is in [docs/safety-model.md](docs/safety-model.md).

Highlights:

- **Confirmation tokens are bound to arguments.** The guard hashes the tool name and canonical arguments. A code
  issued for "bed 100 C" cannot approve "bed 110 C", and a code issued for deleting `a.pdf` cannot delete `b.pdf`.
  Codes are single-use and expire.
- **The model cannot confirm its own call.** The code goes to stderr and the state dir, not into the tool result.
  The orchestrator also strips any `confirm_token` the model makes up.
- **Defense in depth.** The orchestrator caps autonomous risk (`autonomous_max_risk`) on its side. Tools from
  third-party servers that have no confirmation flow of their own need the approver's yes.
- **Safe exits stay open.** `drone_land`, `drone_rtl`, `printer_pause`, heater-off and outlet-off are low risk,
  so a model can always reach them.
- **Tamper-evident audit.** Every decision goes into a SHA-256-chained JSONL log, with secrets redacted.

## Cost-aware routing (summary)

Tiers live in `config/aegis.example.toml`, ordered from cheapest to strongest. The defaults are a local Ollama
model, then `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`. Prices are in the config and you are
expected to keep them current. The router:

1. scores difficulty;
2. picks the first tier that covers the score;
3. respects the context size, `latency_budget_s` and the cost caps (per request, per caller and remaining daily
   budget);
4. escalates on provider errors or low-confidence answers;
5. records the real cost from usage in a per-day budget file.

Bring your own difficulty estimator or provider with one function or class each. Details:
[docs/architecture.md](docs/architecture.md#router-algorithm).

## Development

```bash
pip install -e ".[dev]"
pytest -q            # unit + MCP protocol tests (stdio subprocesses and streamable HTTP)
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the suite on Ubuntu for Python 3.11–3.13 and
builds the wheel. The tests cover:

- every tool on every server;
- the safety layer: blocked without confirmation, arguments bound to tokens, path traversal including symlink
  escapes, geofence, rate limits and audit tampering;
- router decisions, fallback and escalation;
- provider request/response mapping;
- the orchestrator loop with a scripted fake LLM;
- protocol-level tests that spawn each server and list its tools through a real MCP stdio client session, plus
  one streamable-HTTP session.

## Roadmap

- MCP elicitation as a third confirmation channel, for clients that support it.
- Printer: Bambu Lab (MQTT/LAN) and PrusaLink backends, filament/spool tracking, and first-layer snapshot
  checks with a vision model.
- Drone: mission upload with a fence check on every waypoint, a MAVLink `FENCE_*` parameter sync so the FC
  enforces the same fence, a battery-reserve RTL policy and validated PX4 offboard support.
- NAS: write and move with quotas, ZFS snapshot listing and restore-from-trash.
- Lab: ESPHome native API, power metering via Shelly/Tasmota energy topics, and scheduled scenes.
- Core: streaming responses, a learned difficulty estimator trained from logged outcomes, and per-tier
  prompt-cache awareness in cost estimates.
- Signed audit checkpoints shipped off-box.

## Hardware validation / TODO for the author

None of the following has been done. Everything passing today passes against in-repo fakes.

- [ ] **OctoPrint**: run against a real instance and check the `/api/printer` 409 (disconnected) path, upload
      of a large file, `select`+`print`, and the webcam snapshot URL for your setup.
- [ ] **Moonraker/Klipper**: check `SET_HEATER_TEMPERATURE` heater names (`extruder`, `heater_bed`, a chamber
      sensor name if you have one), the upload `root=gcodes`, and print start/pause/cancel.
- [ ] **Drone, ArduPilot SITL**: run the full flow in [docs/simulators.md](docs/simulators.md) (arm, takeoff,
      goto inside and outside the fence, RTL). Confirm that `MAV_CMD_DO_SET_MODE` and the GUIDED takeoff
      behave as in the fake.
- [ ] **Drone, real vehicle**: start with props off and `--dry-run`, then a tethered or very low fence. Keep
      ArduPilot's own `FENCE_*` and failsafes enabled. Check heartbeat timing over your radio link.
- [ ] **PX4**: mode switching through pymavlink's PX4 map and takeoff altitude semantics.
- [ ] **AEGIS-Link**: implement the protocol on the custom FC firmware and bench-test it over the real serial
      link (props off).
- [ ] **NAS**: SMART on your real disks. `smartctl` usually needs root or the `disk` group, and the JSON
      fields differ by drive and USB bridge (`-d sat`). Check `nas_disk_usage` on SMB and NFS mounts.
- [ ] **TrueNAS / Synology**: confirm the endpoints and auth (API key scope, DSM version, 2FA accounts) on
      your firmware versions.
- [ ] **Lab**: map your real topics (Tasmota `cmnd/stat`, ESPHome, Shelly Gen2 RPC topics differ). Set
      `risk_on` and `auto_off_s` for anything that heats. Check the Home Assistant token scope.
- [ ] **Router**: replace the example prices with current ones, and tune `max_difficulty` thresholds on your
      own traffic with `aegis-orchestrate --explain`.
- [ ] **Claude Desktop/Code**: confirm where each client shows server stderr, so the human can find
      confirmation codes. Consider the `aegis-mcp approve` flow instead.

## Plugging in the private AEGIS

AEGIS itself is private and none of its code is in this repo. [docs/extracting-from-aegis.md](docs/extracting-from-aegis.md)
explains how the private assistant consumes these packages, and how to move further pieces out of it safely.

## License

MIT. See [LICENSE](LICENSE).
