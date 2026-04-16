"""
Chrony NTP Sync — LUCID component that manages a chronyd subprocess
for clock synchronization on edge devices.

Spawns chronyd in foreground mode (-d), polls chronyc for sync status,
and publishes offset/stratum/reachability as MQTT telemetry.

Configuration is loaded from chrony.yaml shipped alongside this module.
To override, set "config_path" in the component context config or place
chrony.yaml next to the agent's working directory.

Device prerequisites (one-time, baked into image):
  1. apt install chrony
  2. sudo setcap cap_sys_time+ep /usr/sbin/chronyd
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lucid_component_base import Component, ComponentContext

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "chrony.yaml"
_CONF_TEMPLATE_PATH = Path(__file__).parent / "chrony.conf.template"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- config loading -----------------------------------------------------------

def _load_chrony_config(context_config: dict[str, Any]) -> dict[str, Any]:
    """Load chrony YAML config with 3-tier resolution.

    1. context_config["config_path"] — explicit override
    2. ./chrony.yaml                 — working directory
    3. <package>/chrony.yaml         — default shipped with component
    """
    explicit = context_config.get("config_path")
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Chrony config not found: {p}")
        logger.info("Loading chrony config from explicit path: %s", p)
        with p.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    local = Path("chrony.yaml")
    if local.is_file():
        logger.info("Loading chrony config from working dir: %s", local.resolve())
        with local.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    if _DEFAULT_CONFIG_PATH.is_file():
        logger.info("Loading chrony config from package default: %s", _DEFAULT_CONFIG_PATH)
        with _DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    logger.warning("No chrony.yaml found — using built-in defaults")
    return {}


# -- chronyc output parsers ---------------------------------------------------

def parse_chronyc_tracking_csv(output: str) -> dict[str, Any] | None:
    """Parse CSV output from ``chronyc -c tracking``.

    Fields (comma-separated):
      ref_id_hex, ref_id_name, stratum, ref_time, system_time,
      last_offset, rms_offset, frequency, residual_freq, skew,
      root_delay, root_dispersion, update_interval, leap_status
    """
    line = output.strip()
    if not line:
        return None

    parts = line.split(",")
    if len(parts) < 14:
        return None

    try:
        return {
            "ref_id": parts[1] if parts[1] else parts[0],
            "stratum": int(parts[2]),
            "offset_ms": round(float(parts[5]) * 1000, 6),
            "rms_offset_ms": round(float(parts[6]) * 1000, 6),
            "frequency_ppm": round(float(parts[7]), 3),
            "root_delay_ms": round(float(parts[10]) * 1000, 6),
            "root_dispersion_ms": round(float(parts[11]) * 1000, 6),
            "update_interval_s": round(float(parts[12]), 1),
            "leap_status": parts[13],
        }
    except (ValueError, IndexError):
        return None


def parse_chronyc_sources_csv(output: str) -> int:
    """Parse CSV output from ``chronyc -c sources`` and return reachability
    of the active (selected) source as a decimal integer (0–255).

    The active source line starts with ``^*`` or ``^=`` (combined/selected).
    Field layout: mode_state, name, stratum, poll, reach, ...
    """
    for line in output.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 5:
            continue
        mode_state = parts[0]
        # ^* = current best, ^= = combined
        if mode_state in ("^*", "^="):
            try:
                return int(parts[4], 8)
            except ValueError:
                return 0
    return 0


# -- component ----------------------------------------------------------------

class ChronyComponent(Component):
    """Manages a chronyd subprocess and publishes NTP sync telemetry.

    Retained: metadata, status, state, cfg.
    Telemetry: offset_ms, stratum, reachability.
    Commands: start_sync, stop_sync, reset, ping, cfg/set.
    """

    _DEFAULT_TELEMETRY_CFG = {
        "offset_ms": {"enabled": True, "interval_s": 10, "change_threshold_percent": 5.0},
        "stratum": {"enabled": True, "interval_s": 30, "change_threshold_percent": 0.0},
        "reachability": {"enabled": True, "interval_s": 30, "change_threshold_percent": 0.0},
    }

    def __init__(self, context: ComponentContext) -> None:
        super().__init__(context)
        self._log = context.logger()

        cfg = _load_chrony_config(dict(context.config))

        self._ntp_server: str = str(cfg.get("ntp_server", "192.168.0.100"))
        self._telemetry_interval_s: float = float(cfg.get("telemetry_interval_s", 10))
        self._max_offset_ms: float = float(cfg.get("max_offset_ms", 10.0))

        # Runtime state
        self._chronyd_proc: subprocess.Popen | None = None
        self._sync_active: bool = False
        self._tracking_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._latest_tracking: dict[str, Any] = {}
        self._tracking_lock = threading.Lock()
        self._runtime_conf_path: str | None = None

    @property
    def component_id(self) -> str:
        return "chrony"

    def capabilities(self) -> list[str]:
        return ["reset", "ping", "start_sync", "stop_sync"]

    def metadata(self) -> dict[str, Any]:
        out = super().metadata()
        out["capabilities"] = self.capabilities()
        out["ntp_server"] = self._ntp_server
        return out

    def get_state_payload(self) -> dict[str, Any]:
        with self._tracking_lock:
            return {
                "sync_active": self._sync_active,
                "ntp_server": self._ntp_server,
                "offset_ms": self._latest_tracking.get("offset_ms"),
                "stratum": self._latest_tracking.get("stratum"),
                "reachability": self._latest_tracking.get("reachability"),
                "ref_id": self._latest_tracking.get("ref_id"),
                "last_poll_at": self._latest_tracking.get("polled_at"),
            }

    def get_cfg_payload(self) -> dict[str, Any]:
        return {
            "ntp_server": self._ntp_server,
            "telemetry_interval_s": self._telemetry_interval_s,
            "max_offset_ms": self._max_offset_ms,
        }

    def schema(self) -> dict[str, Any]:
        s = copy.deepcopy(super().schema())
        s["publishes"]["state"]["fields"].update({
            "sync_active": {"type": "boolean"},
            "ntp_server": {"type": "string"},
            "offset_ms": {"type": "number"},
            "stratum": {"type": "integer"},
            "reachability": {"type": "integer"},
            "ref_id": {"type": "string"},
            "last_poll_at": {"type": "string", "format": "date-time"},
        })
        s["publishes"]["cfg"]["fields"].update({
            "ntp_server": {"type": "string"},
            "telemetry_interval_s": {"type": "number"},
            "max_offset_ms": {"type": "number"},
        })
        s["subscribes"].update({
            "cmd/start_sync": {"fields": {}},
            "cmd/stop_sync": {"fields": {}},
        })
        return s

    # -- lifecycle ------------------------------------------------------------

    def _start(self) -> None:
        if not shutil.which("chronyd"):
            raise RuntimeError(
                "chronyd not found on PATH. Install chrony and ensure "
                "CAP_SYS_TIME is set: sudo setcap cap_sys_time+ep /usr/sbin/chronyd"
            )
        if not shutil.which("chronyc"):
            raise RuntimeError("chronyc not found on PATH. Install chrony.")

        self._start_chronyd()
        self._start_tracking_thread()
        self._publish_all_retained()

    def _stop(self) -> None:
        self._stop_tracking_thread()
        self._stop_chronyd()
        self._log.info("Chrony component stopped")

    # -- chronyd subprocess management ----------------------------------------

    def _write_runtime_conf(self) -> str:
        """Write a runtime chrony.conf from the template and return its path."""
        template = _CONF_TEMPLATE_PATH.read_text(encoding="utf-8")
        content = template.format(
            ntp_server=self._ntp_server,
            agent_id=self.context.agent_id,
        )
        fd, path = tempfile.mkstemp(
            prefix=f"lucid-chrony-{self.context.agent_id}-", suffix=".conf",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _start_chronyd(self) -> None:
        """Spawn chronyd in foreground mode."""
        if self._chronyd_proc is not None and self._chronyd_proc.poll() is None:
            self._log.info("chronyd already running (pid %d)", self._chronyd_proc.pid)
            self._sync_active = True
            return

        self._runtime_conf_path = self._write_runtime_conf()
        self._log.info(
            "Starting chronyd with server %s (conf: %s)",
            self._ntp_server, self._runtime_conf_path,
        )

        self._chronyd_proc = subprocess.Popen(
            ["chronyd", "-d", "-f", self._runtime_conf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        self._sync_active = True

    def _stop_chronyd(self) -> None:
        """Stop the chronyd subprocess (SIGINT → SIGKILL)."""
        proc = self._chronyd_proc
        if proc is None or proc.poll() is not None:
            self._chronyd_proc = None
            self._sync_active = False
            self._cleanup_runtime_conf()
            return

        self._log.info("Stopping chronyd (pid %d)", proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._log.warning("chronyd did not exit in 10s, sending SIGKILL")
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

        self._chronyd_proc = None
        self._sync_active = False
        self._cleanup_runtime_conf()

    def _cleanup_runtime_conf(self) -> None:
        if self._runtime_conf_path and os.path.exists(self._runtime_conf_path):
            try:
                os.unlink(self._runtime_conf_path)
            except OSError:
                pass
            self._runtime_conf_path = None

    # -- telemetry polling ----------------------------------------------------

    def _start_tracking_thread(self) -> None:
        self._stop_event.clear()
        self._tracking_thread = threading.Thread(
            target=self._tracking_loop, daemon=True,
        )
        self._tracking_thread.start()

    def _stop_tracking_thread(self) -> None:
        self._stop_event.set()
        if self._tracking_thread is not None:
            self._tracking_thread.join(timeout=self._telemetry_interval_s + 2)
            self._tracking_thread = None

    def _tracking_loop(self) -> None:
        """Poll chronyc periodically and publish telemetry."""
        while not self._stop_event.wait(timeout=self._telemetry_interval_s):
            tracking = self._poll_chronyc()
            if tracking is None:
                continue

            with self._tracking_lock:
                self._latest_tracking = tracking

            self.publish_telemetry("offset_ms", tracking["offset_ms"])
            self.publish_telemetry("stratum", tracking["stratum"])
            self.publish_telemetry("reachability", tracking["reachability"])

            if abs(tracking["offset_ms"]) > self._max_offset_ms:
                self._log.warning(
                    "Clock offset %.3f ms exceeds threshold %.1f ms",
                    tracking["offset_ms"], self._max_offset_ms,
                )

            self.publish_state()

    def _poll_chronyc(self) -> dict[str, Any] | None:
        """Run chronyc tracking + sources and return merged result."""
        try:
            tracking_result = subprocess.run(
                ["chronyc", "-c", "tracking"],
                capture_output=True, text=True, timeout=10,
            )
            if tracking_result.returncode != 0:
                self._log.warning(
                    "chronyc tracking failed: %s", tracking_result.stderr.strip(),
                )
                return None

            parsed = parse_chronyc_tracking_csv(tracking_result.stdout)
            if parsed is None:
                return None

            # Get reachability from sources
            sources_result = subprocess.run(
                ["chronyc", "-c", "sources"],
                capture_output=True, text=True, timeout=10,
            )
            if sources_result.returncode == 0:
                parsed["reachability"] = parse_chronyc_sources_csv(sources_result.stdout)
            else:
                parsed["reachability"] = 0

            parsed["polled_at"] = _utc_iso()
            return parsed

        except Exception as exc:
            self._log.warning("chronyc poll error: %s", exc)
            return None

    # -- retained publishing --------------------------------------------------

    def _publish_all_retained(self) -> None:
        self.publish_metadata()
        self.publish_schema()
        self.publish_status()
        self.publish_state()
        self.publish_cfg()

    # -- command handlers -----------------------------------------------------

    def on_cmd_ping(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""
        self.publish_result("ping", request_id, ok=True, error=None)

    def on_cmd_reset(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        with self._tracking_lock:
            self._latest_tracking.clear()

        self.publish_state()
        self.publish_result("reset", request_id, ok=True, error=None)

    def on_cmd_start_sync(self, payload_str: str) -> None:
        """Start chronyd subprocess if not already running."""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        if self._sync_active and self._chronyd_proc and self._chronyd_proc.poll() is None:
            self.publish_result("start_sync", request_id, ok=True, error=None)
            return

        try:
            self._start_chronyd()
            if self._tracking_thread is None or not self._tracking_thread.is_alive():
                self._start_tracking_thread()
            self.publish_state()
            self.publish_result("start_sync", request_id, ok=True, error=None)
        except Exception as exc:
            self._log.error("Failed to start chronyd: %s", exc)
            self.publish_result("start_sync", request_id, ok=False, error=str(exc))

    def on_cmd_stop_sync(self, payload_str: str) -> None:
        """Stop chronyd subprocess if running."""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        if not self._sync_active:
            self.publish_result("stop_sync", request_id, ok=True, error=None)
            return

        self._stop_tracking_thread()
        self._stop_chronyd()
        self.publish_state()
        self.publish_result("stop_sync", request_id, ok=True, error=None)

    def on_cmd_cfg_set(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            set_dict = payload.get("set") or {}
        except json.JSONDecodeError:
            request_id = ""
            set_dict = {}

        if not isinstance(set_dict, dict):
            self.publish_cfg_set_result(
                request_id=request_id,
                ok=False,
                applied=None,
                error="payload 'set' must be an object",
                ts=_utc_iso(),
            )
            return

        applied: dict[str, Any] = {}
        rejected: dict[str, str] = {}

        if "telemetry_interval_s" in set_dict:
            val = float(set_dict["telemetry_interval_s"])
            if val < 1:
                rejected["telemetry_interval_s"] = "must be >= 1"
            else:
                self._telemetry_interval_s = val
                applied["telemetry_interval_s"] = self._telemetry_interval_s

        if "max_offset_ms" in set_dict:
            val = float(set_dict["max_offset_ms"])
            if val <= 0:
                rejected["max_offset_ms"] = "must be > 0"
            else:
                self._max_offset_ms = val
                applied["max_offset_ms"] = self._max_offset_ms

        if "ntp_server" in set_dict:
            rejected["ntp_server"] = "cannot be changed at runtime; restart required"

        known_keys = {"telemetry_interval_s", "max_offset_ms", "ntp_server"}
        for key in set_dict:
            if key not in known_keys:
                rejected[key] = "unknown config key"

        self.publish_state()
        self.publish_cfg()
        self.publish_cfg_set_result(
            request_id=request_id,
            ok=True,
            applied=applied if applied else None,
            error=json.dumps({"rejected": rejected}) if rejected else None,
            ts=_utc_iso(),
        )
