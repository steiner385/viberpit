"""Startup hardening for the processes that hold ViberPit's secrets (#3386).

Sessions run as the same user as the daemon and the edge, so two things a session's
shell could otherwise do are closed here. Defence in depth: a process running as that
user can still read that user's own files (the account store, ``~/.config/gh``, a
group-readable ``/etc/viberpit/daemon.env``); real containment is the sandbox (D-23).

* :func:`make_non_dumpable` — ``prctl(PR_SET_DUMPABLE, 0)``. ``/proc/<pid>/environ``
  (which holds ``VIBERPIT_MASTER_KEY``), ``/proc/<pid>/mem``, ptrace attach and core
  dumps become root-only for this process. Called from ``server/__init__.py`` for the
  MAIN process of a systemd unit (the edge, the daemon, the deploy controller, the Codex
  authority; :func:`harden_if_service`), as early as Python can, and unconditionally by
  the daemon's entrypoint. Nothing else is hardened: the flag resets on ``execve``, so
  the ``claude``/``codex`` children stay inspectable (process adoption reads their
  ``environ``), and a ``python`` a session runs — a dev edge, a pytest — inherits the
  unit's env but is not its main process, so it stays dumpable (dev-server detection
  attributes its listener through ``/proc/<pid>/fd``). The trade-off: ``py-spy``,
  ``gdb`` or ``lsof`` against the daemon or edge now needs ``sudo``, and the process
  cannot read its own ``/proc/self/environ`` or write its own
  ``/proc/self/oom_score_adj`` (nothing does).
  Linux only; a best-effort no-op elsewhere.
* :func:`refuse_empty_secrets` — refuse to start when a secret-shaped namespace name
  (``env_compat.is_secret_shaped``: ``DB_URL``, ``MASTER_KEY``, ``SESSION_SECRET``, any
  ``*_KEY``/``*_TOKEN``, …) is SET BUT EMPTY. A session's env carries the namespace
  blanked to ``""``, and ``Settings`` reads ``""`` as unset (``env_ignore_empty``), so
  without this a blanked env would boot an edge or daemon on the default SQLite store
  instead of failing.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Mapping

log = logging.getLogger(__name__)

#: ``<linux/prctl.h>``.
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4


class EmptySecretError(RuntimeError):
    """A secret-shaped ``VIBERPIT_*`` name is set to the empty string."""


def make_non_dumpable() -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)`` on Linux. True when the flag is now off; False on
    any other platform or failure (never raises)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            log.warning("prctl(PR_SET_DUMPABLE, 0) failed: errno %d", ctypes.get_errno())
            return False
        return bool(libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0)
    except (OSError, AttributeError):
        return False


def harden_if_service(environ: Mapping[str, str] | None = None, *,
                      pid: int | None = None) -> bool:
    """:func:`make_non_dumpable` when this process is a systemd unit's MAIN process:
    ``SYSTEMD_EXEC_PID`` (systemd >= 248) equals our pid. Not ``INVOCATION_ID``: every
    descendant inherits that — the session CLIs and anything an agent runs under them —
    and hardening those hid an in-session dev server's sockets from dev-server
    detection. A hand-run process stays debuggable too."""
    env = os.environ if environ is None else environ
    me = os.getpid() if pid is None else pid
    return make_non_dumpable() if env.get("SYSTEMD_EXEC_PID") == str(me) else False


def empty_secret_names(environ: Mapping[str, str]) -> list[str]:
    """The secret-shaped namespace names in *environ* that are set to ``""``, sorted."""
    from viberpit.env_compat import LEGACY_PREFIX, NEW_PREFIX, is_secret_shaped
    return sorted(name for name, value in environ.items()
                  if value == "" and name.startswith((NEW_PREFIX, LEGACY_PREFIX))
                  and is_secret_shaped(name, ""))


def refuse_empty_secrets(environ: Mapping[str, str] | None = None, *, process: str) -> None:
    """Raise :class:`EmptySecretError` if any secret-shaped namespace name is set but
    empty. Names only in the message — never a value."""
    names = empty_secret_names(os.environ if environ is None else environ)
    if names:
        raise EmptySecretError(
            f"refusing to start the {process}: {', '.join(names)} "
            f"{'is' if len(names) == 1 else 'are'} set but empty. An empty secret is never "
            "a valid configuration; this env looks like a session's (#3386). Unset the "
            "name or give it its real value.")
