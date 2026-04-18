# Chrony NTP Sync

> **Package:** `lucid-component-chrony` | **Version:** dynamic (setuptools_scm) | **Entry Point:** `chrony = "lucid_component_chrony.component:ChronyComponent"`

## Overview

LUCID edge component that manages a `chronyd` subprocess for clock synchronization on Raspberry Pi agents. It spawns chronyd in foreground mode (`-d`), polls `chronyc` for sync status at a configurable interval, and publishes offset/stratum/reachability as MQTT telemetry. Belongs to the **component layer** of the LUCID architecture.

Device prerequisites (one-time, baked into image):
1. `apt install chrony`
2. `sudo setcap cap_sys_time+ep /usr/sbin/chronyd`

## Architecture

### Design Patterns

- **Subprocess supervisor** -- spawns `chronyd -d` in foreground mode within its own process group, manages lifecycle with SIGINT/SIGKILL escalation
- **3-tier config resolution** -- explicit path > working directory > package default
- **Template-based config generation** -- renders `chrony.conf.template` with runtime values to a temp file
- **Polling telemetry** -- background thread polls `chronyc -c` (CSV mode) at a configurable interval and publishes parsed metrics

### Architecture Diagram

```
                    MQTT Broker
                        |
    +---------+---------+---------+---------+
    |         |         |         |         |
metadata   status    state      cfg    telemetry/*
    |         |         |         |     (offset_ms, stratum, reachability)
    |         |         |         |         |
+---+---------+---------+---------+---------+---+
|               ChronyComponent                  |
|                                                |
|  _tracking_loop  ─────>  chronyc -c tracking   |
|  (background thread)     chronyc -c sources    |
|                                                |
|  _start_chronyd  ─────>  chronyd -d -f <conf>  |
|  (subprocess in own process group)             |
+------------------------------------------------+
        |                        |
  chrony.conf.template     chrony.yaml
  (rendered to /tmp)       (3-tier config)
```

## Key Classes and Modules

| Module | Class / Function | Responsibility |
|--------|-----------------|----------------|
| `component.py` | `ChronyComponent` | Main component class; manages chronyd subprocess, tracking thread, and MQTT publishing |
| `component.py` | `_load_chrony_config()` | 3-tier YAML config resolution (explicit path > cwd > package default) |
| `component.py` | `parse_chronyc_tracking_csv()` | Parses CSV output from `chronyc -c tracking` into a structured dict with offset, stratum, frequency, root delay/dispersion |
| `component.py` | `parse_chronyc_sources_csv()` | Parses CSV output from `chronyc -c sources` to extract reachability (octal to decimal) of the active source |
| `chrony.yaml` | -- | Default configuration file shipped with the package |
| `chrony.conf.template` | -- | Python format-string template for generating runtime `chrony.conf` |

## Data Flow and Lifecycle

### Startup Sequence

1. Verify `chronyd` and `chronyc` are on PATH (raises `RuntimeError` if missing)
2. Render `chrony.conf.template` with `ntp_server` and `agent_id` to a temp file in `/tmp`
3. Spawn `chronyd -d -f <conf>` in its own process group (`os.setsid` via `preexec_fn`)
4. Start background tracking thread (`_tracking_loop`)
5. Publish all retained MQTT topics (metadata, schema, status, state, cfg)

### Runtime Loop

The `_tracking_loop` runs in a daemon thread, waking every `telemetry_interval_s` seconds (default 10s):

1. Run `chronyc -c tracking` (10s subprocess timeout) and parse CSV output
2. Run `chronyc -c sources` (10s subprocess timeout) and extract reachability of the selected source
3. Store parsed tracking data under `_tracking_lock`
4. Publish telemetry for `offset_ms`, `stratum`, and `reachability`
5. If `abs(offset_ms) > max_offset_ms`, log a warning (informational only, no enforcement)
6. Publish updated state

### Shutdown Sequence

1. Set `_stop_event` to signal the tracking thread to exit
2. Join tracking thread with timeout of `telemetry_interval_s + 2` seconds
3. Send `SIGINT` to the chronyd process group
4. Wait up to 10s for graceful exit; escalate to `SIGKILL` if needed
5. Delete the temporary runtime conf file

## MQTT Topics

All topics are relative to `lucid/agents/{agent_id}/components/chrony/`.

### Retained Topics (QoS 1)

| Suffix | Payload Shape | Description |
|--------|--------------|-------------|
| `metadata` | `{component_id, version, capabilities, ntp_server}` | Published once on start; includes configured NTP server |
| `status` | `{state: "idle\|running\|error"}` | Updated on state change |
| `state` | `{sync_active, ntp_server, offset_ms, stratum, reachability, ref_id, last_poll_at}` | Current sync status; updated every poll cycle |
| `cfg` | `{ntp_server, telemetry_interval_s, max_offset_ms}` | Current configuration values |
| `cfg/logging` | `{log_level}` | Logging config (inherited from base) |
| `cfg/telemetry` | `{offset_ms: {enabled, interval_s, change_threshold_percent}, stratum: {...}, reachability: {...}}` | Per-metric telemetry gating config |

### Stream Topics (QoS 0)

| Suffix | Payload Shape | Description |
|--------|--------------|-------------|
| `logs` | `{level, message}` | Structured log entries |
| `telemetry/offset_ms` | `{value: <float>}` | Clock offset from NTP server in milliseconds |
| `telemetry/stratum` | `{value: <int>}` | NTP stratum of the current source |
| `telemetry/reachability` | `{value: <int>}` | Reachability register (0-255) of the active source |

### Command Topics

| Suffix | Payload | Handler Method | Description |
|--------|---------|---------------|-------------|
| `cmd/start_sync` | `{request_id}` | `on_cmd_start_sync` | Start chronyd if not already running; restarts tracking thread if needed |
| `cmd/stop_sync` | `{request_id}` | `on_cmd_stop_sync` | Stop tracking thread and chronyd subprocess |
| `cmd/reset` | `{request_id}` | `on_cmd_reset` | Clear cached tracking data and republish state |
| `cmd/ping` | `{request_id}` | `on_cmd_ping` | Health check; always returns ok=true |
| `cmd/cfg/set` | `{request_id, set: {key: value, ...}}` | `on_cmd_cfg_set` | Update runtime config (see Configuration for mutable keys) |

### Result Topics

| Suffix | Payload | Description |
|--------|---------|-------------|
| `evt/start_sync/result` | `{request_id, ok, error?}` | Result of start_sync command |
| `evt/stop_sync/result` | `{request_id, ok, error?}` | Result of stop_sync command |
| `evt/reset/result` | `{request_id, ok, error?}` | Result of reset command |
| `evt/ping/result` | `{request_id, ok, error?}` | Result of ping command |
| `evt/cfg/set/result` | `{request_id, ok, applied?, error?}` | Result of cfg/set; reports applied keys and any rejected keys with reasons |

## Configuration

Loaded from `chrony.yaml` via 3-tier resolution:

1. `context.config["config_path"]` -- explicit override path
2. `./chrony.yaml` -- file in the agent's working directory
3. `<package>/chrony.yaml` -- default shipped with the component

| Key | Type | Default | Mutable at Runtime | Description |
|-----|------|---------|-------------------|-------------|
| `ntp_server` | `str` | `"192.168.0.100"` | No (rejected with error) | IP address of the NTP server |
| `telemetry_interval_s` | `float` | `10` | Yes (must be >= 1) | Polling interval for chronyc in seconds |
| `max_offset_ms` | `float` | `10.0` | Yes (must be > 0) | Offset threshold for warning logs (informational only) |
| `config_path` | `str` | -- | No | Explicit path to a chrony.yaml override file |

### Generated chrony.conf Template

```
server {ntp_server} iburst prefer
makestep 1.0 3
driftfile /tmp/lucid-chrony-{agent_id}.drift
```

Written to a temp file at startup; deleted on shutdown.

## Dependencies

| Package | Version Constraint | Purpose |
|---------|-------------------|---------|
| `lucid-component-base` | `v2.1.0` (git tag) | Component SDK: `Component` base class and `ComponentContext` |
| `pyyaml` | `>=6.0` | YAML config file parsing |
| `chrony` (system) | -- | Provides `chronyd` and `chronyc` binaries (must be installed on the device) |

## Telemetry Metrics

| Metric | Type | Default Interval | Threshold | Description |
|--------|------|-----------------|-----------|-------------|
| `offset_ms` | `float` | `0.1s` | `5%` | Clock offset from the NTP server in milliseconds (from `chronyc tracking`) |
| `stratum` | `int` | `0.1s` | `0%` | NTP stratum level of the current time source |
| `reachability` | `int` | `0.1s` | `0%` | Reachability register (0-255) of the active source; derived from octal field in `chronyc sources` |

Note: The telemetry config defaults have `enabled: False`. The actual polling and publishing cadence is governed by `telemetry_interval_s` (default 10s) in the component config. The telemetry gating system (`interval_s` and `change_threshold_percent`) provides additional filtering on top of this.
