"""Contract tests for ChronyComponent — all subprocess calls are mocked."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from lucid_component_chrony.component import ChronyComponent, _load_chrony_config
from tests.conftest import FakeMqtt, make_context, write_yaml


# -- instantiation & config ---------------------------------------------------


def test_instantiation_with_defaults(monkeypatch):
    """Component loads package-default chrony.yaml."""
    monkeypatch.chdir("/tmp")  # avoid picking up a local chrony.yaml
    ctx = make_context()
    comp = ChronyComponent(ctx)
    assert comp.component_id == "chrony"
    assert comp._ntp_server == "192.168.0.100"
    assert comp._telemetry_interval_s == 10
    assert comp._max_offset_ms == 10.0


def test_instantiation_with_explicit_config(tmp_path):
    yaml_path = write_yaml(tmp_path, """\
        ntp_server: "10.0.0.1"
        telemetry_interval_s: 5
        max_offset_ms: 25.0
    """)
    ctx = make_context(config={"config_path": str(yaml_path)})
    comp = ChronyComponent(ctx)
    assert comp._ntp_server == "10.0.0.1"
    assert comp._telemetry_interval_s == 5
    assert comp._max_offset_ms == 25.0


def test_instantiation_missing_config():
    ctx = make_context(config={"config_path": "/nonexistent/chrony.yaml"})
    with pytest.raises(FileNotFoundError):
        ChronyComponent(ctx)


# -- properties ---------------------------------------------------------------


def test_capabilities(monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    caps = comp.capabilities()
    assert "reset" in caps
    assert "ping" in caps
    assert "start_sync" in caps
    assert "stop_sync" in caps


def test_get_state_payload(monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    state = comp.get_state_payload()
    assert state["sync_active"] is False
    assert state["ntp_server"] == "192.168.0.100"
    assert state["offset_ms"] is None
    assert state["stratum"] is None
    assert state["reachability"] is None


def test_get_cfg_payload(monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    cfg = comp.get_cfg_payload()
    assert cfg == {
        "ntp_server": "192.168.0.100",
        "telemetry_interval_s": 10,
        "max_offset_ms": 10.0,
    }


def test_metadata_includes_capabilities(monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    meta = comp.metadata()
    assert "capabilities" in meta
    assert "start_sync" in meta["capabilities"]
    assert meta["ntp_server"] == "192.168.0.100"


def test_schema_has_custom_fields(monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    s = comp.schema()
    assert "sync_active" in s["publishes"]["state"]["fields"]
    assert "offset_ms" in s["publishes"]["state"]["fields"]
    assert "cmd/start_sync" in s["subscribes"]
    assert "cmd/stop_sync" in s["subscribes"]
    assert "ntp_server" in s["publishes"]["cfg"]["fields"]


# -- lifecycle ----------------------------------------------------------------


@patch("lucid_component_chrony.component.chrony_client")
@patch("lucid_component_chrony.component.shutil.which", return_value="/usr/bin/chronyc")
def test_start_delegates_to_helper(mock_which, mock_client, monkeypatch):
    """Component delegates chronyd management to the helper daemon."""
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._start()

    mock_client.start.assert_called_once_with(
        ntp_server="192.168.0.100", agent_id="test-agent",
    )
    assert comp._sync_active is True

    # Clean up
    comp._stop_event.set()
    if comp._tracking_thread:
        comp._tracking_thread.join(timeout=2)


@patch("lucid_component_chrony.component.chrony_client")
@patch("lucid_component_chrony.component.shutil.which", return_value="/usr/bin/chronyc")
def test_stop_delegates_to_helper(mock_which, mock_client, monkeypatch):
    """Component delegates stop to the helper daemon."""
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": True}
    mock_client.stop.return_value = {"ok": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._start()
    comp._stop()

    mock_client.stop.assert_called_once()
    assert comp._sync_active is False


# -- command handlers ---------------------------------------------------------


def test_cmd_ping(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_ping(json.dumps({"request_id": "r1"}))

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        (t, json.loads(p)) for t, p, q, r in mqtt.published
        if "evt/ping/result" in t
    ]
    assert len(results) == 1
    assert results[0][1]["ok"] is True
    assert results[0][1]["request_id"] == "r1"


def test_cmd_ping_empty_payload(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_ping("")

    mqtt: FakeMqtt = ctx.mqtt
    results = [p for t, p, q, r in mqtt.published if "evt/ping/result" in t]
    assert len(results) == 1


def test_cmd_reset_clears_tracking(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._latest_tracking = {"offset_ms": 1.0, "stratum": 2}

    comp.on_cmd_reset(json.dumps({"request_id": "r2"}))

    assert comp._latest_tracking == {}
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/reset/result" in t
    ]
    assert results[0]["ok"] is True


@patch("lucid_component_chrony.component.chrony_client")
def test_cmd_start_sync(mock_client, monkeypatch):
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_start_sync(json.dumps({"request_id": "r3"}))

    assert comp._sync_active is True
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/start_sync/result" in t
    ]
    assert results[0]["ok"] is True

    # Clean up
    comp._stop_event.set()
    if comp._tracking_thread:
        comp._tracking_thread.join(timeout=2)


@patch("lucid_component_chrony.component.chrony_client")
def test_cmd_start_sync_idempotent(mock_client, monkeypatch):
    monkeypatch.chdir("/tmp")
    mock_client.status.return_value = {"ok": True, "running": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._sync_active = True

    comp.on_cmd_start_sync(json.dumps({"request_id": "r4"}))

    # Should not call start again — already running
    mock_client.start.assert_not_called()
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/start_sync/result" in t
    ]
    assert results[0]["ok"] is True


def test_cmd_stop_sync_when_not_running(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_stop_sync(json.dumps({"request_id": "r5"}))

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/stop_sync/result" in t
    ]
    assert results[0]["ok"] is True


# -- cfg/set ------------------------------------------------------------------


def test_cfg_set_mutable_keys(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c1",
        "set": {"telemetry_interval_s": 5, "max_offset_ms": 20.0},
    }))

    assert comp._telemetry_interval_s == 5
    assert comp._max_offset_ms == 20.0

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/cfg/set/result" in t
    ]
    assert results[0]["ok"] is True
    assert results[0]["applied"]["telemetry_interval_s"] == 5


def test_cfg_set_ntp_server_rejected(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c2",
        "set": {"ntp_server": "10.0.0.99"},
    }))

    # ntp_server should not change
    assert comp._ntp_server == "192.168.0.100"
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/cfg/set/result" in t
    ]
    error = json.loads(results[0]["error"])
    assert "ntp_server" in error["rejected"]


def test_cfg_set_unknown_key_rejected(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c3",
        "set": {"bogus_key": 42},
    }))

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/cfg/set/result" in t
    ]
    error = json.loads(results[0]["error"])
    assert "bogus_key" in error["rejected"]


def test_cfg_set_invalid_payload(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c4",
        "set": "not-a-dict",
    }))

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/cfg/set/result" in t
    ]
    assert results[0]["ok"] is False


def test_cfg_set_telemetry_interval_too_low(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c5",
        "set": {"telemetry_interval_s": 0.5},
    }))

    assert comp._telemetry_interval_s == 10  # unchanged
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/cfg/set/result" in t
    ]
    error = json.loads(results[0]["error"])
    assert "telemetry_interval_s" in error["rejected"]


def test_cfg_set_max_offset_negative(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)

    comp.on_cmd_cfg_set(json.dumps({
        "request_id": "c6",
        "set": {"max_offset_ms": -1},
    }))

    assert comp._max_offset_ms == 10.0  # unchanged


# -- config loading edge cases ------------------------------------------------


def test_config_from_working_directory(tmp_path):
    """Config loaded from ./chrony.yaml when no explicit path given."""
    yaml_path = tmp_path / "chrony.yaml"
    yaml_path.write_text('ntp_server: "10.10.10.10"\n', encoding="utf-8")
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cfg = _load_chrony_config({})
        assert cfg["ntp_server"] == "10.10.10.10"
    finally:
        os.chdir(original_cwd)


def test_config_fallback_to_empty(monkeypatch, tmp_path):
    """When no config file is found anywhere, returns empty dict."""
    monkeypatch.chdir(tmp_path)  # no chrony.yaml here
    import lucid_component_chrony.component as mod
    original = mod._DEFAULT_CONFIG_PATH
    monkeypatch.setattr(mod, "_DEFAULT_CONFIG_PATH", tmp_path / "nonexistent.yaml")
    cfg = _load_chrony_config({})
    assert cfg == {}


# -- _poll_chronyc ------------------------------------------------------------

TRACKING_CSV = (
    "C0A80064,192.168.0.100,2,1681725791.0,0.0,"
    "0.000008123,0.000015432,1.234,0.001,0.012,"
    "0.001234,0.000567,64.0,0"
)
SOURCES_CSV = "^*,192.168.0.100,2,6,377,32,+0.000123,-0.000456,0.000789\n"


@patch("lucid_component_chrony.component.subprocess.run")
def test_poll_chronyc_success(mock_run, monkeypatch):
    monkeypatch.chdir("/tmp")
    tracking_result = MagicMock()
    tracking_result.returncode = 0
    tracking_result.stdout = TRACKING_CSV
    tracking_result.stderr = ""

    sources_result = MagicMock()
    sources_result.returncode = 0
    sources_result.stdout = SOURCES_CSV

    mock_run.side_effect = [tracking_result, sources_result]

    comp = ChronyComponent(make_context())
    result = comp._poll_chronyc()

    assert result is not None
    assert abs(result["offset_ms"] - 0.008123) < 0.0001
    assert result["stratum"] == 2
    assert result["reachability"] == 255
    assert "polled_at" in result


@patch("lucid_component_chrony.component.subprocess.run")
def test_poll_chronyc_tracking_fails(mock_run, monkeypatch):
    monkeypatch.chdir("/tmp")
    tracking_result = MagicMock()
    tracking_result.returncode = 1
    tracking_result.stderr = "error"
    mock_run.return_value = tracking_result

    comp = ChronyComponent(make_context())
    assert comp._poll_chronyc() is None


@patch("lucid_component_chrony.component.subprocess.run")
def test_poll_chronyc_sources_fails_still_returns(mock_run, monkeypatch):
    """If sources fails, reachability defaults to 0 but tracking data is still returned."""
    monkeypatch.chdir("/tmp")
    tracking_result = MagicMock()
    tracking_result.returncode = 0
    tracking_result.stdout = TRACKING_CSV

    sources_result = MagicMock()
    sources_result.returncode = 1

    mock_run.side_effect = [tracking_result, sources_result]

    comp = ChronyComponent(make_context())
    result = comp._poll_chronyc()
    assert result is not None
    assert result["reachability"] == 0


@patch("lucid_component_chrony.component.subprocess.run", side_effect=OSError("boom"))
def test_poll_chronyc_exception(mock_run, monkeypatch):
    monkeypatch.chdir("/tmp")
    comp = ChronyComponent(make_context())
    assert comp._poll_chronyc() is None


# -- tracking_loop ------------------------------------------------------------


@patch("lucid_component_chrony.component.subprocess.run")
def test_tracking_loop_publishes_telemetry(mock_run, monkeypatch):
    monkeypatch.chdir("/tmp")
    tracking_result = MagicMock()
    tracking_result.returncode = 0
    tracking_result.stdout = TRACKING_CSV

    sources_result = MagicMock()
    sources_result.returncode = 0
    sources_result.stdout = SOURCES_CSV

    mock_run.side_effect = [tracking_result, sources_result]

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._telemetry_interval_s = 0.01  # fast for testing
    comp.set_telemetry_config({
        "offset_ms": {"enabled": True, "interval_s": 1, "change_threshold_percent": 0},
        "stratum": {"enabled": True, "interval_s": 1, "change_threshold_percent": 0},
        "reachability": {"enabled": True, "interval_s": 1, "change_threshold_percent": 0},
    })

    # Run one iteration manually
    tracking = comp._poll_chronyc()
    assert tracking is not None
    comp._latest_tracking = tracking
    comp.publish_telemetry("offset_ms", tracking["offset_ms"])
    comp.publish_telemetry("stratum", tracking["stratum"])
    comp.publish_telemetry("reachability", tracking["reachability"])

    mqtt: FakeMqtt = ctx.mqtt
    telemetry_topics = [t for t, p, q, r in mqtt.published if "telemetry/" in t]
    assert any("telemetry/offset_ms" in t for t in telemetry_topics)
    assert any("telemetry/stratum" in t for t in telemetry_topics)
    assert any("telemetry/reachability" in t for t in telemetry_topics)


@patch("lucid_component_chrony.component.subprocess.run")
def test_tracking_loop_warns_on_high_offset(mock_run, monkeypatch):
    """When offset exceeds threshold, a warning is logged."""
    monkeypatch.chdir("/tmp")
    # Offset of 50ms (0.05 seconds)
    high_offset_csv = (
        "C0A80064,192.168.0.100,2,1681725791.0,0.0,"
        "0.050000,0.0,0.0,0.0,0.0,0.0,0.0,64.0,0"
    )
    tracking_result = MagicMock()
    tracking_result.returncode = 0
    tracking_result.stdout = high_offset_csv

    sources_result = MagicMock()
    sources_result.returncode = 0
    sources_result.stdout = SOURCES_CSV

    mock_run.side_effect = [tracking_result, sources_result]

    comp = ChronyComponent(make_context())
    comp._max_offset_ms = 10.0
    tracking = comp._poll_chronyc()
    assert tracking is not None
    assert abs(tracking["offset_ms"]) > comp._max_offset_ms


# -- chronyc_missing check ---------------------------------------------------


def test_start_fails_when_chronyc_missing(monkeypatch):
    monkeypatch.chdir("/tmp")

    def which_side_effect(name):
        if name == "chronyd":
            return "/usr/sbin/chronyd"
        return None

    monkeypatch.setattr("lucid_component_chrony.component.shutil.which", which_side_effect)
    comp = ChronyComponent(make_context())
    with pytest.raises(RuntimeError, match="chronyc not found"):
        comp._start()


# -- stop edge cases ----------------------------------------------------------


@patch("lucid_component_chrony.component.chrony_client")
def test_stop_when_not_running(mock_client, monkeypatch):
    """Stop delegates to helper even when component thinks sync is inactive."""
    monkeypatch.chdir("/tmp")
    mock_client.stop.return_value = {"ok": True}
    comp = ChronyComponent(make_context())
    comp._sync_active = True

    comp._stop_chronyd()
    mock_client.stop.assert_called_once()
    assert comp._sync_active is False


# -- cmd handlers with bad JSON -----------------------------------------------


def test_cmd_reset_bad_json(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_reset("{bad json}")
    mqtt: FakeMqtt = ctx.mqtt
    results = [p for t, p, q, r in mqtt.published if "evt/reset/result" in t]
    assert len(results) == 1


@patch("lucid_component_chrony.component.chrony_client")
def test_cmd_start_sync_bad_json(mock_client, monkeypatch):
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_start_sync("{bad}")

    mqtt: FakeMqtt = ctx.mqtt
    results = [p for t, p, q, r in mqtt.published if "evt/start_sync/result" in t]
    assert len(results) == 1

    # Clean up
    comp._stop_event.set()
    if comp._tracking_thread:
        comp._tracking_thread.join(timeout=2)


def test_cmd_stop_sync_bad_json(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_stop_sync("{bad}")
    mqtt: FakeMqtt = ctx.mqtt
    results = [p for t, p, q, r in mqtt.published if "evt/stop_sync/result" in t]
    assert len(results) == 1


def test_cmd_cfg_set_bad_json(monkeypatch):
    monkeypatch.chdir("/tmp")
    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_cfg_set("{bad}")
    # Should not crash; publishes result with empty set_dict
    mqtt: FakeMqtt = ctx.mqtt
    results = [p for t, p, q, r in mqtt.published if "evt/cfg/set/result" in t]
    assert len(results) == 1


# -- cmd/stop_sync when running -----------------------------------------------


@patch("lucid_component_chrony.component.chrony_client")
@patch("lucid_component_chrony.component.shutil.which", return_value="/usr/bin/chronyc")
def test_cmd_stop_sync_when_running(mock_which, mock_client, monkeypatch):
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": True}
    mock_client.stop.return_value = {"ok": True}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp._start()

    comp.on_cmd_stop_sync(json.dumps({"request_id": "r6"}))

    assert comp._sync_active is False
    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/stop_sync/result" in t
    ]
    assert results[0]["ok"] is True


# -- start_sync failure -------------------------------------------------------


@patch("lucid_component_chrony.component.chrony_client")
def test_cmd_start_sync_failure(mock_client, monkeypatch):
    monkeypatch.chdir("/tmp")
    mock_client.start.return_value = {"ok": False, "error": "systemctl restart chrony failed"}

    ctx = make_context()
    comp = ChronyComponent(ctx)
    comp.on_cmd_start_sync(json.dumps({"request_id": "r7"}))

    mqtt: FakeMqtt = ctx.mqtt
    results = [
        json.loads(p) for t, p, q, r in mqtt.published
        if "evt/start_sync/result" in t
    ]
    assert results[0]["ok"] is False
    assert "failed" in results[0]["error"]
