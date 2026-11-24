# Security policy

aegis-mcp lets software drive **physical hardware**: heaters, motors, propellers, mains relays and the
files on your NAS. Treat bypasses of the safety layer as security vulnerabilities, even when no
"classic" exploit is involved.

## Supported versions

Only the latest commit on `main` is supported. The project is alpha and has been tested against simulators
only (see the README's Status section).

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/coopersimpson101-debug/aegis-mcp/security/advisories/new)
("Security" tab → "Report a vulnerability") and include:

- the affected commit, server and tool;
- the exact tool calls or MCP messages needed to reproduce, ideally against an in-repo simulator
  (`aegis-mcp sim ...`, `src/aegis_mcp/sim/`);
- the physical consequence you believe is possible (for example "bed heated above `max_bed_c`" or "takeoff outside the fence").

You should get an acknowledgement within 7 days. Please allow up to 90 days for a fix before public
disclosure. Fixes for safety-critical issues are prioritised over everything else.

**Never test a suspected bypass on real hardware that could cause harm.** Use the simulators, `--dry-run`, props
off, and heaters disconnected.

## What counts as a vulnerability

These are in scope, because they break the guarantees in [docs/safety-model.md](docs/safety-model.md):

- executing a `high`/`critical` action without a valid human confirmation code;
- reusing a code, using it after expiry, or using it for different arguments than it was issued for;
- a confirmation code appearing anywhere the model can read it (tool results, resources, errors);
- exceeding a domain limit: temperature hard max, G-code scan, geofence, takeoff altitude, mode allowlist or
  lab allowlists;
- NAS access outside a configured root (traversal, symlinks, deny globs), or writes to a read-only root;
- making a safe exit (`drone_land`, `drone_rtl`, `printer_pause`, heater off, outlet off) unreachable;
- forging or silently truncating the hash-chained audit log, or secrets leaking into it.

Known limitations that are documented in
[docs/safety-model.md → What this does not protect against](docs/safety-model.md#what-this-does-not-protect-against)
are not vulnerabilities by themselves. Neither are attacks that require a compromised host running the
server.

## Safety model pointers

- Guard pipeline, risk levels and confirmation flow: [docs/safety-model.md](docs/safety-model.md)
- Architecture and orchestrator policy (`autonomous_max_risk`, approver): [docs/architecture.md](docs/architecture.md)
- AEGIS-Link serial protocol (custom flight controllers): [docs/aegis-link-protocol.md](docs/aegis-link-protocol.md)
- Keep the hardware's own protections on (ArduPilot `FENCE_*` and failsafes, printer thermal runaway
  protection, breakers). aegis-mcp is an additional layer, not a replacement.
