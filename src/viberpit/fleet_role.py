"""The per-host fleet role — THE reader of ``VIBERPIT_DAEMON_ROLE``.

``primary`` (the default) runs every background loop, owns fleet-global
automation and is the one process that may refresh a Claude OAuth token;
``worker`` serves dispatched sessions only (#840) and reads shared account state
without ever rotating it (#852, #2625).

This lives in the core package rather than the server because both layers key
policy off the role: ``server.sessions.orchestrator`` gates its loops on it,
``viberpit.account_registry`` gates manifest writes on it and
``viberpit.accounts`` gates token refresh on it. Before this module two of those
read the env var themselves and disagreed on what an invalid value meant. Any
other value raises: silently falling back to ``primary`` would double-drive the
fleet on a typo, and silently becoming ``worker`` would quietly stop every
automation — both are worse than a loud failed boot.
"""
from __future__ import annotations

import os

ROLE_ENV = "VIBERPIT_DAEMON_ROLE"
PRIMARY = "primary"
WORKER = "worker"


def daemon_role() -> str:
    """Return this process's role: ``"primary"`` (default) or ``"worker"``."""
    role = os.environ.get(ROLE_ENV, PRIMARY).strip().lower()
    if role not in (PRIMARY, WORKER):
        raise ValueError(f"{ROLE_ENV}={role!r} — expected 'primary' or 'worker'")
    return role


def is_worker() -> bool:
    """True on a runner host that serves dispatched sessions only."""
    return daemon_role() == WORKER
