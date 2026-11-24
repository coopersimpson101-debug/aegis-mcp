# Simulators and fakes

Everything in this repo is tested without hardware. These are the stand-ins.

| Device | Fake | Fidelity |
|--------|------|----------|
| OctoPrint | `aegis_mcp.sim.printers.FakeOctoPrint` (`aegis-mcp sim octoprint`) | Implements the endpoints the backend uses, API-key check, toy heater/print model |
| Moonraker/Klipper | `FakeMoonraker` (`aegis-mcp sim moonraker`) | Same for Moonraker endpoints and `SET_HEATER_TEMPERATURE` |
| MAVLink vehicle | `aegis_mcp.sim.vehicle.FakeMavlinkVehicle` (`aegis-mcp sim vehicle`) | Real MAVLink over TCP, simplified ArduCopter behaviour: heartbeats, position, battery, GPS, arm checks, GUIDED takeoff/goto, LAND, RTL, auto-disarm. **No flight dynamics** |
| Custom FC | `FakeAegisLinkFC` | In-memory AEGIS-Link transport, can drop/corrupt frames |
| MQTT broker + Tasmota plugs | `aegis_mcp.sim.lab.InMemoryBroker`, `make_demo_lab()` (`aegis-mcp-lab --demo`) | Wildcards, retained messages, plugs that answer `cmnd/<x>/POWER` with `stat/<x>/POWER` |
| Home Assistant | `FakeHomeAssistant` | `/api/states/<id>`, `/api/services/<domain>/<service>`, bearer token |
| NAS | pytest `tmp_path` trees + `fake_smartctl` with JSON fixtures in `tests/fixtures/` | Real filesystem ops; SMART parsing from smartctl 7.x JSON shape |
| TrueNAS / Synology | fakes in `tests/test_nas.py` | Only the endpoints used |

HTTP fakes can run in-process (`app.transport()` -> `httpx.MockTransport`) or on a real socket
(`app.serve(port=...)`), so you can point the real MCP servers at them:

```bash
aegis-mcp sim octoprint --port 5000          # prints the API key it expects (default sim-key)
AEGIS_PRINTER_API_KEY=sim-key aegis-mcp-printer --url http://127.0.0.1:5000
```

## ArduPilot SITL (recommended before any real flight)

The in-repo fake vehicle checks the MAVLink plumbing; SITL checks behaviour against real
ArduCopter firmware. On Linux/WSL:

```bash
git clone --recurse-submodules https://github.com/ArduPilot/ardupilot.git
cd ardupilot && Tools/environment_install/install-prereqs-ubuntu.sh -y && . ~/.profile
cd ArduCopter && sim_vehicle.py -v ArduCopter --console --map -w
# SITL serves MAVLink on tcp:127.0.0.1:5760 (and udp 14550 via MAVProxy)
```

Then:

```bash
aegis-mcp-drone --config config/drone.example.toml   # geofence defaults match SITL's default home
```

SITL starts at the CMAC field near Canberra (-35.363262, 149.165237); the example geofence is
centred there. Wait for "EKF3 IMU0 is using GPS" in the console before arming. Try:
telemetry -> arm (confirm) -> takeoff 10 (confirm) -> goto inside the fence -> goto outside (blocked) -> RTL.

PX4 SITL (`make px4_sitl gazebo-classic`) is reachable with `--connection udpin:0.0.0.0:14540`.
PX4 support is best-effort: mode names come from pymavlink's PX4 map and `MAV_CMD_NAV_TAKEOFF`
semantics differ slightly (PX4 uses AMSL altitude in some versions). Validate in SITL first.

## A real MQTT broker

```bash
docker run -it --rm -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
mosquitto_pub -t lab/env/temperature -m 22.5 -r
aegis-mcp-lab --config config/lab.example.toml
```
