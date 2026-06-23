"""Well-known socket path for the session daemon — single source of truth for edge + daemon."""
import os

DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"


def sessiond_socket() -> str:
    """The daemon UDS path: CCSWITCH_SESSIOND_SOCKET if set, else the well-known default."""
    return os.environ.get("CCSWITCH_SESSIOND_SOCKET", DEFAULT_SOCKET_PATH)
