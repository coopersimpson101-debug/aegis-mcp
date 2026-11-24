# Contributing to aegis-mcp

Thanks for your interest! New backends, devices and safety checks are especially welcome.

## Ground rules

- **Safety-first, always.** Every tool that changes the physical world goes through `Guard` with an honest
  risk level, and its domain limits are validated *before* execution. Safe exits (land, RTL, pause, heater off,
  outlet off) must stay low risk.
- **Test against simulators.** Every tool needs a test against an in-repo fake (`src/aegis_mcp/sim/`). If you
  add a backend, add a fake for it.
- **Be honest about validation.** Do not claim hardware support in docs until it has been run on that hardware.
  Record what was run in the README's Hardware validation table.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                                         # unit, safety and MCP protocol tests
python examples/offline_demo.py                   # end-to-end, no hardware, no API key
python scripts/make_figures.py                    # regenerate README figures from real runs
```

## Adding a tool

1. Pick the risk level: `read`, `low`, `medium`, `high` or `critical`. If the risk depends on the arguments,
   compute it per call.
2. Put domain checks in `validate()` so they raise `SafetyViolation`. They run before dry-run and confirmation.
3. Accept an optional `confirm_token` if the tool can ever be `high` or above.
4. Add tests for: the allowed path, the blocked path, the confirmation path (a code bound to arguments,
   single-use), and the audit record.
5. Document it in the tool reference table in the README.

## Pull requests

- Keep PRs focused and describe the safety impact (new risks, changed limits).
- CI (tests on Python 3.11–3.13 plus a wheel build) must pass.
- Report security or safety bypasses privately; see [SECURITY.md](SECURITY.md).

By contributing you agree that your contributions are licensed under the MIT License.
