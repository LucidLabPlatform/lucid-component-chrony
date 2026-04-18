"""
Chrony helper daemon — runs as root, manages the chronyd subprocess.

Listens on a Unix socket for JSON-line commands from the LUCID component
(running as normal user). Handles start, stop, status, and ping.

Start: lucid-chrony-helper (or python -m lucid_component_chrony.helper_server)
Socket: LUCID_CHRONY_SOCKET or /run/lucid/chrony.sock
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
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
        self._chronyd_proc: subprocess.Popen | None = None
        self._runtime_conf_path: str | None = None

    def start(self, ntp_server: str, agent_id: str) -> None:
        with self._lock:
            if self._chronyd_proc is not None and self._chronyd_proc.poll() is None:
                logger.info("chronyd already running (pid %d)", self._chronyd_proc.pid)
                return

            # Write runtime conf from template
            template = _CONF_TEMPLATE_PATH.read_text(encoding="utf-8")
            content = template.format(ntp_server=ntp_server, agent_id=agent_id)
            fd, path = tempfile.mkstemp(
                prefix=f"lucid-chrony-{agent_id}-", suffix=".conf",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            self._runtime_conf_path = path

            logger.info(
                "Starting chronyd with server %s (conf: %s)", ntp_server, path,
            )
            self._chronyd_proc = subprocess.Popen(
                ["chronyd", "-d", "-f", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )

    def stop(self) -> None:
        with self._lock:
            proc = self._chronyd_proc
            if proc is None or proc.poll() is not None:
                self._chronyd_proc = None
                self._cleanup_conf()
                return

            logger.info("Stopping chronyd (pid %d)", proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("chronyd did not exit in 10s, sending SIGKILL")
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
            except ProcessLookupError:
                pass

            self._chronyd_proc = None
            self._cleanup_conf()

    def status(self) -> dict:
        with self._lock:
            if self._chronyd_proc is not None and self._chronyd_proc.poll() is None:
                return {"running": True, "pid": self._chronyd_proc.pid}
            return {"running": False, "pid": None}

    def _cleanup_conf(self) -> None:
        if self._runtime_conf_path and os.path.exists(self._runtime_conf_path):
            try:
                os.unlink(self._runtime_conf_path)
            except OSError:
                pass
            self._runtime_conf_path = None


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
            agent_id = req.get("agent_id")
            if not ntp_server or not agent_id:
                return {"id": rid, "ok": False, "error": "ntp_server and agent_id required"}
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
    except OSError:
        pass
    server.listen(4)
    logger.info("Listening on %s", path)

    state = HelperState()
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
