"""
IPC client for the chrony helper daemon.

Used by the LUCID component (running as normal user) to send start/stop
commands to the root-owned helper. Connects to the Unix socket, sends
JSON-line requests, reads JSON-line responses. Reconnects on each call.
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any

from .protocol import (
    CMD_PING,
    CMD_START,
    CMD_STATUS,
    CMD_STOP,
    DEFAULT_SOCKET_PATH,
)


def _socket_path() -> str:
    return os.environ.get("LUCID_CHRONY_SOCKET", DEFAULT_SOCKET_PATH)


def _request(cmd: str, **params: Any) -> dict:
    path = _socket_path()
    req = {"id": 1, "cmd": cmd, **params}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10.0)
        sock.connect(path)
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return {"ok": False, "error": "connection closed"}
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode("utf-8")
        return json.loads(line)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def start(ntp_server: str, agent_id: str) -> dict:
    """Ask helper to spawn chronyd with the given NTP server."""
    return _request(CMD_START, ntp_server=ntp_server, agent_id=agent_id)


def stop() -> dict:
    """Ask helper to stop the chronyd subprocess."""
    return _request(CMD_STOP)


def status() -> dict:
    """Query whether chronyd is running. Returns {ok, running, pid}."""
    return _request(CMD_STATUS)


def ping() -> dict:
    """Health check the helper daemon."""
    return _request(CMD_PING)
