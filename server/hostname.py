"""This process's fleet identity — dependency-light so the deploy controller can import it
without pulling in the orchestrator (which drags the whole session engine)."""
from __future__ import annotations

import os
import socket


def self_host_name() -> str:
    """The ``hosts.name`` this process answers to.

    ``CCSWITCH_HOST_NAME`` wins when set (operator-managed registries can use any name).
    The fallback is the kernel hostname's first label, which matches how auto-registered
    hosts are named (the first label of the tailnet MagicDNS endpoint). A worker whose
    identity doesn't match its registry row rehydrates zero sessions after a restart —
    set the env var explicitly on renamed hosts.
    """
    name = os.environ.get("CCSWITCH_HOST_NAME", "").strip()
    if name:
        return name
    return socket.gethostname().split(".")[0]
