"""
Chrony helper daemon — runs as root, manages the system chrony service.

Listens on a Unix socket for JSON-line commands from the LUCID component
(running as normal user). Handles start, stop, status, and ping.

Start: lucid-chrony-helper (or python -m lucid_component_chrony.helper_server)
Socket: LUCID_CHRONY_SOCKET or /run/lucid/chrony.sock

start  → writes /etc/chrony/chrony.conf with the requested NTP server
         (typically 10.205.10.16, the LUCID internal NTP server), then runs
         ``systemctl restart chrony``.
stop   → restores chrony to the OS default (pool.ntp.org) and restarts.
         The service is never stopped so the device clock stays synced.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
from pathlib import Path

from .protocol import (
    CMD_PING,
    CMD_START,
    CMD_STATUS,
    CMD_STOP,
    DEFAULT_SOCKET_PATH,
)

logger = logging.getLogger(__name__)

_CONF_TEMPLATE_PATH = Path(__file__).parent / "chrony.conf.template"
_CHRONY_CONF_PATH = Path("/etc/chrony/chrony.conf")

_FALLBACK_CONF = "pool pool.ntp.org iburst\nmakestep 1.0 3\nrtcsync\n"

LUCID_GROUP = "lucid"
SOCKET_MODE = 0o660


def _get_socket_path() -> str:
    return os.environ.get("LUCID_CHRONY_SOCKET", DEFAULT_SOCKET_PATH)


def _gid_for(name: str) -> int | None:
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        return None


class HelperState:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # -- public commands ------------------------------------------------------

    def start(self, ntp_server: str, agent_id: str) -> None:
        with self._lock:
            self._write_conf(ntp_server)
            self._restart_chrony()
            logger.info("Chrony configured for server %s", ntp_server)

    def stop(self) -> None:
        with self._lock:
            _CHRONY_CONF_PATH.write_text(_FALLBACK_CONF, encoding="utf-8")
            os.chmod(str(_CHRONY_CONF_PATH), 0o644)
            self._restart_chrony()
            logger.info("Chrony reset to OS default (pool.ntp.org)")

    def status(self) -> dict:
        with self._lock:
            r = subprocess.run(
                ["systemctl", "is-active", "chrony"],
                capture_output=True, text=True, timeout=10,
            )
            running = r.stdout.strip() == "active"
            return {"running": running, "pid": None}

    # -- internal helpers -----------------------------------------------------

    def _write_conf(self, ntp_server: str) -> None:
        template = _CONF_TEMPLATE_PATH.read_text(encoding="utf-8")
        content = template.format(ntp_server=ntp_server)
        _CHRONY_CONF_PATH.write_text(content, encoding="utf-8")
        os.chmod(str(_CHRONY_CONF_PATH), 0o644)
        logger.info("Wrote %s (server=%s)", _CHRONY_CONF_PATH, ntp_server)

    def _restart_chrony(self) -> None:
        r = subprocess.run(
            ["systemctl", "restart", "chrony"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"systemctl restart chrony failed: "
                f"{(r.stderr or r.stdout or '').strip()}"
            )


def _handle_request(state: HelperState, req: dict) -> dict:
    rid = req.get("id")
    cmd = req.get("cmd")
    if not cmd:
        return {"id": rid, "ok": False, "error": "missing cmd"}
    try:
        if cmd == CMD_PING:
            return {"id": rid, "ok": True}
        if cmd == CMD_START:
            ntp_server = req.get("ntp_server")
            agent_id = req.get("agent_id", "")
            if not ntp_server:
                return {"id": rid, "ok": False, "error": "ntp_server required"}
            state.start(ntp_server, agent_id)
            return {"id": rid, "ok": True}
        if cmd == CMD_STOP:
            state.stop()
            return {"id": rid, "ok": True}
        if cmd == CMD_STATUS:
            return {"id": rid, "ok": True, **state.status()}
        return {"id": rid, "ok": False, "error": f"unknown cmd: {cmd}"}
    except Exception as e:
        logger.exception("Command %s failed", cmd)
        return {"id": rid, "ok": False, "error": str(e)}


def _serve_connection(state: HelperState, conn: socket.socket) -> None:
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    resp = {"id": None, "ok": False, "error": str(e)}
                else:
                    resp = _handle_request(state, req)
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_server() -> None:
    path = _get_socket_path()
    sock_dir = os.path.dirname(path)
    if sock_dir:
        os.makedirs(sock_dir, mode=0o755, exist_ok=True)
        try:
            gid = _gid_for(LUCID_GROUP)
            if gid is not None:
                os.chown(sock_dir, 0, gid)
                os.chmod(sock_dir, 0o775)
        except OSError:
            pass

    if os.path.exists(path):
        os.unlink(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        gid = _gid_for(LUCID_GROUP)
        if gid is not None:
            os.chown(path, 0, gid)
            os.chmod(path, SOCKET_MODE)
        else:
            # No lucid group — allow any local user to connect
            os.chmod(path, 0o666)
    except OSError:
        pass
    server.listen(4)
    logger.info("Listening on %s", path)

    state = HelperState()
    logger.info("stop-sync will restore chrony to pool.ntp.org")

    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        t = threading.Thread(target=_serve_connection, args=(state, conn), daemon=True)
        t.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    run_server()


if __name__ == "__main__":
    main()
