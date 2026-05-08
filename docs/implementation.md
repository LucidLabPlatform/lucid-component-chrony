# Chrony NTP Sync

> **Package:** `lucid-component-chrony` | **Version:** dynamic (setuptools_scm) | **Entry Point:** `chrony = "lucid_component_chrony.component:ChronyComponent"`

## Overview

LUCID edge component that manages NTP clock synchronization on Raspberry Pi agents. It delegates chrony daemon lifecycle to a privileged helper process (`lucid-chrony-helper`) over a Unix socket, polls `chronyc` for sync status at a configurable interval, and publishes offset/stratum/reachability as MQTT telemetry. Belongs to the **component layer** of the LUCID architecture.

Device prerequisites (one-time, baked into image):
1. `apt install chrony`
2. `sudo lucid-chrony-helper-installer --install-once`

## Architecture

### Design Patterns

- **Helper-daemon IPC** — all chrony configuration and `systemctl` interactions are delegated to the root-owned `lucid-chrony-helper` daemon via a Unix socket (`/run/lucid/chrony.sock`). The agent process does **not** require root.
- **4-tier config resolution** — `LUCID_CHRONY_NTP_SERVER` env var > explicit path > working directory > package default
- **Polling telemetry** — background tracking thread polls `chronyc -c` (CSV mode) at a configurable interval and publishes parsed metrics

### Architecture Diagram

```
                   MQTT Broker
                       |
   +---------+---------+---------+---------+
   |         |         |         |         |
metadata   status    state      cfg    telemetry/*
   |         |         |         |     (offset_ms, stratum, reachability)
   |         |         |         |         |
+--+---------+---------+---------+---------+--+
|              ChronyComponent                 |
|                                             |
|  _tracking_loop  ────>  chronyc -c tracking  |
|  (background thread)    chronyc -c sources   |
|                                             |
|  _start_chronyd  ──> chrony_client.start()  |
|  _stop_chronyd   ──> chrony_client.stop()   |
+---------------------------------------------+
                        |
                Unix socket (JSON-line)
                /run/lucid/chrony.sock
                        |
          +-----------------------------+
          |   lucid-chrony-helper       |
          |   (runs as root, systemd)   |
          |                             |
          |  start → writes             |
          |    /etc/chrony/chrony.conf  |
          |    systemctl restart chrony |
          |                             |
          |  stop  → restores           |
          |    pool.ntp.org fallback    |
          |    systemctl restart chrony |
          +-----------------------------+
```

## Key Classes and Modules

| Module | Class / Function | Responsibility |
|--------|-----------------|----------------|
| `component.py` | `ChronyComponent` | Main component class; manages tracking thread and MQTT publishing; delegates chrony configuration to helper via `chrony_client` |
| `component.py` | `_load_chrony_config()` | 4-tier YAML config resolution (`LUCID_CHRONY_NTP_SERVER` env var > explicit path > cwd > package default) |
| `component.py` | `parse_chronyc_tracking_csv()` | Parses CSV output from `chronyc -c tracking` into a structured dict with offset, stratum, frequency, root delay/dispersion |
| `component.py` | `parse_chronyc_sources_csv()` | Parses CSV output from `chronyc -c sources` to extract reachability (octal to decimal) of the active source |
| `client.py` | `start()` / `stop()` / `ping()` | Unix-socket client to `lucid-chrony-helper`; connects, sends a JSON-line request, reads JSON-line response |
| `helper_server.py` | `HelperState` | Root-owned daemon state; handles `start` (renders `chrony.conf`, calls `systemctl restart chrony`) and `stop` (restores `pool.ntp.org` fallback, restarts) |
| `helper_server.py` | `run_server()` | Event loop that accepts Unix socket connections and dispatches `_handle_request` per connection |
| `protocol.py` | constants | Socket path (`/run/lucid/chrony.sock`) and command names (`start`, `stop`, `ping`) shared between client and server |
| `chrony.yaml` | -- | Default configuration file shipped with the package |
| `chrony.conf.template` | -- | Python format-string template rendered by the helper when `start` is received |

## Data Flow and Lifecycle

### Startup Sequence

1. Verify `chronyc` is on PATH (raises `RuntimeError` if missing)
2. Ping `lucid-chrony-helper` via Unix socket — if unreachable, publish retained topics and raise `RuntimeError` with install instructions
3. Start background tracking thread (`_tracking_loop`)
4. Publish all retained MQTT topics (metadata, schema, status, state, cfg)

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

The tracking thread stopping does **not** stop the chrony service — chrony continues running on the host. The helper daemon (`lucid-chrony-helper`) remains active and manages the system chrony service independently of the component lifecycle.

### Command Flow: `start-sync`

1. Component receives `cmd/start-sync` with `{request_id}`
2. `on_cmd_start_sync` calls `_start_chronyd()`, which calls `chrony_client.start(ntp_server, agent_id)`
3. Client opens Unix socket to `/run/lucid/chrony.sock`, sends `{"cmd": "start", "ntp_server": "...", "agent_id": "..."}`
4. Helper's `HelperState.start()` renders `chrony.conf` from template with `{ntp_server}`, writes to `/etc/chrony/chrony.conf`, runs `systemctl restart chrony`
5. Helper responds `{"ok": true}`; component publishes `evt/start-sync/result {request_id, ok: true}`
6. The tracking thread picks up the new sync state on its next poll cycle

### Command Flow: `stop-sync`

1. Component receives `cmd/stop-sync` with `{request_id}`
2. `on_cmd_stop_sync` calls `_stop_chronyd()`, which calls `chrony_client.stop()`
3. Client sends `{"cmd": "stop"}` over the Unix socket
4. Helper's `HelperState.stop()` writes the fallback conf (`pool pool.ntp.org iburst`) to `/etc/chrony/chrony.conf`, runs `systemctl restart chrony`
5. Helper responds `{"ok": true}`; component publishes `evt/stop-sync/result {request_id, ok: true}`
6. Tracking thread reflects the restored sync state on next poll

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
| `logs` | `{count, lines: [{ts, level, logger, message, exception}]}` | Structured log entries (canonical MQTTLogHandler envelope) |
| `telemetry/offset_ms` | `{value: <float>}` | Clock offset from NTP server in milliseconds |
| `telemetry/stratum` | `{value: <int>}` | NTP stratum of the current source |
| `telemetry/reachability` | `{value: <int>}` | Reachability register (0-255) of the active source |

### Command Topics

| Suffix | Payload | Handler Method | Description |
|--------|---------|---------------|-------------|
| `cmd/start_sync` | `{request_id}` | `on_cmd_start_sync` | Tell helper to render `chrony.conf` for the configured `ntp_server` and restart chrony |
| `cmd/stop_sync` | `{request_id}` | `on_cmd_stop_sync` | Tell helper to restore OS default (`pool.ntp.org`) and restart chrony |
| `cmd/reset` | `{request_id}` | `on_cmd_reset` | Clear cached tracking data and republish state |
| `cmd/ping` | `{request_id}` | `on_cmd_ping` | Health check; always returns ok=true |
| `cmd/cfg/set` | `{request_id, set: {key: value, ...}}` | `on_cmd_cfg_set` | Update runtime config (see Configuration for mutable keys) |

> **Note on topic naming:** `topics.txt` currently uses underscores (`cmd/start_sync`, `cmd/stop_sync`). The component's `capabilities()` list uses hyphens (`start-sync`, `stop-sync`), matching all other LUCID components. The Central Command `command_dispatch._normalize_action` bridges the underscore form used in experiment templates. This inconsistency is tracked as audit finding R-HIGH-02 and will be resolved in a future `topics.txt` cleanup pass.

### Result Topics

| Suffix | Payload | Description |
|--------|---------|-------------|
| `evt/start_sync/result` | `{request_id, ok, error?}` | Result of start-sync command |
| `evt/stop_sync/result` | `{request_id, ok, error?}` | Result of stop-sync command |
| `evt/reset/result` | `{request_id, ok, error?}` | Result of reset command |
| `evt/ping/result` | `{request_id, ok, error?}` | Result of ping command |
| `evt/cfg/set/result` | `{request_id, ok, applied?, error?}` | Result of cfg/set; reports applied keys and any rejected keys with reasons |

## Configuration

Loaded from `chrony.yaml` via 4-tier resolution:

1. `LUCID_CHRONY_NTP_SERVER` env var — per-device override (highest priority)
2. `context.config["config_path"]` — explicit override path
3. `./chrony.yaml` — file in the agent's working directory
4. `<package>/chrony.yaml` — default shipped with the component

| Key | Type | Default | Mutable at Runtime | Description |
|-----|------|---------|-------------------|-------------|
| `ntp_server` | `str` | `"pool.ntp.org"` | Yes (`on_cmd_cfg_set` accepts it) | NTP server address used when `start-sync` is called |
| `telemetry_interval_s` | `float` | `10` | Yes (must be >= 1) | Polling interval for chronyc in seconds |
| `max_offset_ms` | `float` | `10.0` | Yes (must be > 0) | Offset threshold for warning logs (informational only) |
| `config_path` | `str` | -- | No | Explicit path to a chrony.yaml override file |

## Helper Daemon

The `lucid-chrony-helper` daemon is a separate process installed once per device as a systemd unit. It runs as root and is the only process that touches `/etc/chrony/chrony.conf` or calls `systemctl restart chrony`.

| Aspect | Detail |
|--------|--------|
| Entry point | `lucid-chrony-helper` script (`helper_server.main()`) |
| Socket path | `/run/lucid/chrony.sock` (or `LUCID_CHRONY_SOCKET` env override) |
| Socket permissions | `0o660`, owned by root:`lucid` group so agent user can connect |
| Install command | `sudo lucid-chrony-helper-installer --install-once` |
| Protocol | JSON-line over `AF_UNIX SOCK_STREAM`; one connection per call |
| `start` command | Renders `chrony.conf.template` → `/etc/chrony/chrony.conf`, runs `systemctl restart chrony` |
| `stop` command | Writes hardcoded `pool pool.ntp.org iburst` fallback → `/etc/chrony/chrony.conf`, runs `systemctl restart chrony`. Chrony is **never stopped** — the device clock always stays synced. |
| `ping` command | Returns `{"ok": true}` with no side effects |

## Dependencies

| Package | Version Constraint | Purpose |
|---------|-------------------|---------|
| `lucid-component-base` | `v2.3.1` (git tag) | Component SDK: `Component` base class and `ComponentContext` |
| `pyyaml` | `>=6.0` | YAML config file parsing |
| `chrony` (system) | -- | Provides `chronyc` binary (polling only; daemon lifecycle managed by helper) |

## Telemetry Metrics

| Metric | Type | Default Interval | Threshold | Description |
|--------|------|-----------------|-----------|-------------|
| `offset_ms` | `float` | `0.1s` | `5%` | Clock offset from the NTP server in milliseconds (from `chronyc tracking`) |
| `stratum` | `int` | `0.1s` | `0%` | NTP stratum level of the current time source |
| `reachability` | `int` | `0.1s` | `0%` | Reachability register (0-255) of the active source; derived from octal field in `chronyc sources` |

Note: The telemetry config defaults have `enabled: False`. The actual polling and publishing cadence is governed by `telemetry_interval_s` (default 10s) in the component config. The telemetry gating system (`interval_s` and `change_threshold_percent`) provides additional filtering on top of this.
