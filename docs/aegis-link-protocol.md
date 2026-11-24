# AEGIS-Link v1 serial protocol

For custom flight-controller firmware that does not speak MAVLink. The host side is
`aegis_mcp.drone.aegislink.AegisLinkBackend`. A reference FC implementation (in Python, for
tests) is `aegis_mcp.sim.vehicle.FakeAegisLinkFC`. Port it to your firmware.

## Framing

```
$AGL,<seq>,<CMD>[,<arg>...]*<CK>\r\n
```

* ASCII only. Fields are comma-separated and may not contain `, * $ \r \n`.
* `seq`: decimal 0-65535, chosen by the host, wraps around. Every reply echoes the `seq` of the
  command it answers. The host ignores replies with a stale `seq`.
* `CK`: two uppercase hex digits, the XOR of every byte between `$` and `*` (NMEA style).
  Example: `$AGL,1,PING*6B`.
* Floats: decimal, `.` separator, up to 7 decimals for lat/lon.
* Default link: 115200 baud 8N1. Anything pyserial can open works (`/dev/ttyUSB0`, `COM7`,
  `socket://host:port`, `rfc2217://...`).

## Host -> FC commands

| CMD | Args | Meaning |
|-----|------|---------|
| `PING` | - | liveness; reply `PONG,<firmware_version>` |
| `TELEM` | - | reply `TEL,...` (below) |
| `ARM` | `[FORCE]` | arm motors |
| `DISARM` | `[FORCE]` | disarm; without FORCE the FC must refuse when airborne |
| `MODE` | `<name>` | e.g. `GUIDED`, `LOITER`, `LAND`, `RTL` |
| `TAKEOFF` | `<alt_m>` | climb to altitude above home |
| `LAND` | - | land in place |
| `RTL` | - | return to launch and land |
| `GOTO` | `<lat>,<lon>,<alt_m>` | fly to position (altitude above home) |

## FC -> host replies

| Reply | Args |
|-------|------|
| `ACK` | `<CMD>` |
| `NAK` | `<CMD>,<REASON>` e.g. `NO_GPS`, `IN_AIR`, `NOT_ARMED`, `NOT_FLYING`, `UNKNOWN_CMD` |
| `PONG` | `<firmware_version>` |
| `TEL` | `<lat>,<lon>,<rel_alt_m>,<armed 0/1>,<mode>,<batt_v>,<batt_pct>,<sats>,<fix_type>,<in_air 0/1>` |

## Timing and retries

The host waits `timeout_s` (default 2 s) per attempt and retries `retries` times (default 2) with a
**new** `seq`. Frames with a bad checksum are dropped. Commands should therefore be idempotent on
the FC side (re-sending `TAKEOFF,10` while already climbing to 10 m must be harmless).

## Safety expectations on the FC

The MCP server enforces geofence, mode allowlist and confirmations *before* sending, but the FC
must still implement its own failsafes: link-loss -> RTL/land, low battery, hard altitude ceiling,
and refusing `DISARM` in flight without `FORCE`.

## Example session

```
> $AGL,1,PING*6B
< $AGL,1,PONG,skyfc-1.0*..
> $AGL,2,ARM*..
< $AGL,2,ACK,ARM*..
> $AGL,3,TAKEOFF,10*..
< $AGL,3,ACK,TAKEOFF*..
> $AGL,4,TELEM*..
< $AGL,4,TEL,47.3977420,8.5455940,9.8,1,GUIDED,16.4,92,14,3,1*..
```

(`..` stands for the computed checksum.)
