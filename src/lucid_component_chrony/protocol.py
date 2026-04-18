"""
IPC protocol constants for the chrony helper daemon.

Unix socket path and command names shared between helper_server and client.
"""

CMD_START = "start"
CMD_STOP = "stop"
CMD_STATUS = "status"
CMD_PING = "ping"

DEFAULT_SOCKET_PATH = "/run/lucid/chrony.sock"
