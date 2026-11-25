# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-25

First public release of MCP servers for physical hardware with a human-in-the-loop safety layer, plus the AEGIS orchestrator and cost-routing core.

### Added
- MCP servers: printer (OctoPrint, Moonraker), drone (MAVLink), NAS, lab (MQTT, Home Assistant)
- Safety layer: risk levels, allowlists, dry-run, single-use argument-bound confirmation codes, geofence, hash-chained audit log
- aegis_core: cost-aware model router with budgets and escalation; multi-server orchestrator
- In-repo simulators for every device, including a fake ArduCopter speaking real MAVLink

[0.1.0]: https://github.com/coopersimpson101-debug/aegis-mcp/releases/tag/v0.1.0
