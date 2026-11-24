# Plugging your private AEGIS into aegis-mcp

This repository was written as the open-source half of AEGIS: the hardware MCP servers and the
router/orchestrator core, built clean-room so nothing private (prompts, memory, personal data,
device addresses, keys) has to be published. **No AEGIS source code is in this repo.** This guide
describes how the private assistant should consume it, and how to move pieces out of AEGIS later.

## Target shape

```
private AEGIS repo                         public aegis-mcp (this repo)
-----------------------------------        -----------------------------------
voice / UI / memory / persona       ---->  aegis_core.Orchestrator   (agent loop)
private tools (calendar, email...)  ---->  aegis_core.Router         (model choice + budget)
your configs + secrets (env vars)          aegis_mcp.{printer,drone,nas,lab} MCP servers
your FC firmware                    <----  AEGIS-Link protocol spec + reference fake
```

AEGIS depends on `aegis-mcp` as a normal package (`pip install aegis-mcp[all]` or a git
submodule / path dependency during development). AEGIS never imports server internals; it talks
to the hardware servers over MCP, exactly as Claude Desktop would.

## Step 1 - run the servers as AEGIS's hardware layer

1. Copy `config/*.example.toml` into the private repo (e.g. `aegis-private/config/hw/`) and fill in
   real hosts. Secrets go in env vars only (`AEGIS_PRINTER_API_KEY`, `AEGIS_HA_TOKEN`,
   `AEGIS_MQTT_PASSWORD`, `AEGIS_TRUENAS_API_KEY`, ...).
2. Decide the transport per device:
   * same machine as AEGIS: stdio (AEGIS spawns the server);
   * device-adjacent box (Pi next to the printer, companion computer on the drone, the NAS itself):
     `--transport streamable-http --host 0.0.0.0` behind a VPN or authenticating proxy.
3. Point the orchestrator at them with `[[servers]]` entries (see `config/aegis.example.toml`).

## Step 2 - replace AEGIS's model-calling code with the router

Wherever AEGIS currently calls a model directly, build a `RouteRequest` and call `Router.complete()`:

```python
from aegis_core import Router, RouteRequest, Message, load_config

cfg = load_config("config/aegis.toml")
router = Router(cfg.router, cfg.providers)
routed = await router.complete(RouteRequest(messages=[Message("user", text)], latency_budget_s=4))
reply, tier, cost = routed.response.text, routed.tier.name, routed.cost_usd
```

* Voice turns: pass a `latency_budget_s` so the router stays on fast tiers.
* Background jobs (summaries, log analysis): pass `min_tier="deep"` or a `cost_budget_usd`.
* If AEGIS already has a better difficulty signal (intent classifier, conversation state), pass it as
  `Router(..., estimator=my_estimator)`; the signature is `(RouteRequest, input_tokens) -> (score, features)`.
* If AEGIS talks to a provider not covered here, implement the `Provider` protocol
  (`async complete(ChatRequest) -> ChatResponse`, raise `ProviderError` on failure) and register it
  under a name used by a tier.

## Step 3 - use the orchestrator for tool-using turns

```python
from aegis_core import Orchestrator

async with Orchestrator(router, {s.name: s.target() for s in cfg.servers},
                        policy=cfg.policy, approver=aegis_confirm, system_prompt=AEGIS_PERSONA) as orch:
    result = await orch.run(user_text, history=conversation_messages)
```

* `approver` is where AEGIS's UI belongs: speak "Arming needs confirmation. Read me the code from your
  phone/console", or push a notification with approve/deny buttons. Return `True/False` for pre-call
  approvals, or the code / request id string for server confirmations.
* Private tools (calendar, email, ...) can be exposed as additional MCP servers, or AEGIS can keep its
  own tool loop and use only the router. Both work.
* Keep AEGIS's persona/memory prompt private: pass it as `system_prompt`.

## Step 4 - move code from AEGIS into this repo (when you want to)

For each AEGIS module you consider publishing:

1. Grep it for personal data: names, emails, IPs/hostnames, coordinates of your home, API keys, device
   serial numbers, prompt text you consider private. Replace with config values.
2. Re-home it behind one of the existing seams:
   * a new hardware backend -> subclass `PrinterBackend` / `DroneBackend`, or add an MQTT device kind;
   * a new server -> copy the shape of `aegis_mcp/lab/server.py`: `build_server(cfg, guard=...)`, every
     tool wrapped in `guard.run(...)`, `tool_kwargs(Risk...)` on the decorator, a fake in `aegis_mcp/sim/`;
   * routing logic -> an estimator or provider plugin, not edits to `Router`.
3. Add tests against a fake. CI must stay hardware-free.
4. Run `git log -p` on the private history you are extracting from before copying; never `git subtree`
   private history into this public repo.

## Firmware

Your own flight-controller firmware plugs in via `docs/aegis-link-protocol.md`. Port the reference
behaviour in `aegis_mcp/sim/vehicle.py::FakeAegisLinkFC` to C/C++ on the FC, then run
`aegis-mcp-drone --serial-port /dev/ttyUSB0` against a bench-mounted FC with **props removed**.

## Checklist before publishing anything new

- [ ] No personal names, emails, IPs, coordinates, serials or keys (`git grep -nE "@|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"`)
- [ ] New actuating tools have a risk level and go through `guard.run`
- [ ] Fake/simulator + tests exist and pass in CI
- [ ] README tool tables updated
