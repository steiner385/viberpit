"""Where this process's JSON log file lives — resolved PER HOST, never on a share.

#1010: the pre-fix default was ``~/.claude-accounts/logs``, chosen by
``os.access("/var/log", os.W_OK)``. That predicate asks whether the process may
write *the parent* ``/var/log``, which is root-owned, and both units run
``User=tony`` — so it was always false and every process fell through to the home
fallback. On the deployed fleet ``~/.claude-accounts`` is an sshfs mount of the
authority's directory, so rocky's daemon and dobby's edge were appending to, and
independently rotating, ONE ``RotatingFileHandler`` target over FUSE. When #1009
needed rocky's daemon logs there was nothing recoverable to read.

The fix is to ask the questions that actually matter:

* **Writable?** of the log directory itself, not of its parent — the units now
  declare ``LogsDirectory=viberpit``, so systemd creates ``/var/log/viberpit``
  owned by the service user on each host.
* **Local?** via :mod:`server.fsmounts`, the same mount-table reader that refuses
  a ``CODEX_HOME`` on sshfs (#1005). Resolved through symlinks, because a
  ``~/.local/state`` pointed at the fleet's share is the deployed shape.

Locality is a property of the *unit* here, not a hope about where ``$HOME``
resolves on a given runner: every candidate below is a directory systemd creates
per host. The mount check is the backstop for an operator-set log directory and
for dev machines, which have no units at all.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from server import hostpaths
from server.fsmounts import Mounts, filesystem_type_for, is_network_fstype


def log_file_name(process: str) -> str:
    """The JSON log file for *process* ("edge" | "daemon").

    One writer per file, always: a ``RotatingFileHandler`` renames the live file
    out from under any other writer at rollover, so the edge and the daemon — same
    host, two processes — must not share one. Combined with a per-host directory
    this makes the file unique per (host, process), which is the whole of #1010.

    The stem is ``hostpaths.HOST_STEM`` — the same stem as /var/log/<app>, and
    ultimately the brand module, the one place the product name is spelled. The
    pre-#1010 name was the last unrenamed legacy log filename (#1591).
    """
    return f"{hostpaths.HOST_STEM}-{process}.jsonl"


def is_shared_dir(path: str | Path, *, mounts: Mounts | None = None) -> bool:
    """True when *path* is NOT provably on a host-local filesystem.

    Fails closed, like :func:`server.codex.process.assert_local_filesystem`: an
    *undeterminable* mount table is not evidence of locality. The cost of being
    wrong differs, though, and that is why this returns a bool instead of raising
    — a refused log directory is not a refused process. Since the daemon gained
    its ``configure_logging()`` call, the stdout→journald handler is always
    attached, so "no file sink" still means "the journal has every line".

    ``mounts`` overrides the live mount table, the same injection seam
    ``assert_local_filesystem`` takes, so a test can describe a fleet it is not
    running on.
    """
    fstype = filesystem_type_for(path, mounts=mounts)
    return fstype is None or is_network_fstype(fstype)


def _can_write(path: Path) -> bool:
    """Whether *path* is creatable-and-writable, without creating anything.

    The directory usually does not exist yet (``configure_logging`` mkdirs it), so
    walk up to the nearest existing ancestor and ask about that one. Probing by
    actually creating it would leave a directory behind on a candidate we then
    reject — the same "refuse before you write" ordering ``ensure_codex_home``
    keeps.
    """
    probe = path
    while True:
        if probe.exists():
            return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
        if probe.parent == probe:  # reached the filesystem root
            return False
        probe = probe.parent


def _candidates() -> tuple[Path, ...]:
    """Default log directories, best first. Each is host-local by construction.

    1. ``/var/log/viberpit`` — ``LogsDirectory=viberpit`` in the units.
    2. ``/var/lib/viberpit/logs`` — ``StateDirectory=viberpit`` in the daemon
       units; the backstop for a host whose ``LogsDirectory=`` has not been
       installed yet (the units ship in this change, the hosts adopt them on the
       next deploy).
    3. ``$XDG_STATE_HOME/viberpit/logs`` — the dev-machine shape, and the reason
       the mount check exists: ``$HOME`` may itself be networked.
    """
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return (
        Path(hostpaths.LOG_DIR),
        Path(hostpaths.STATE_DIR) / "logs",
        Path(xdg) / hostpaths.HOST_STEM / "logs",
    )


def _resolve(mounts: Mounts | None) -> str:
    for candidate in _candidates():
        try:
            if _can_write(candidate) and not is_shared_dir(candidate, mounts=mounts):
                return str(candidate)
        except OSError:  # pragma: no cover - an unstattable candidate is just not usable
            continue
    return ""


@lru_cache(maxsize=1)
def _resolve_once() -> str:
    return _resolve(None)


def default_log_dir(*, mounts: Mounts | None = None) -> str:
    """The log directory to use when the ``*_LOG_DIR`` env override is unset.

    Returns ``""`` when no candidate is both writable and provably local, which
    ``configure_logging`` reads as "stdout only". Never raises and never creates
    anything: this runs inside a settings default factory, on every process.

    Memoised, because that factory runs on every ``Settings()`` — ~50 call sites,
    some per-request — and reading the mount table costs more than the rest of the
    default put together. The answer is a process-lifetime constant in practice:
    the file handler is attached once, at startup. An explicit ``mounts`` is never
    cached; it describes a fleet this process is not on.
    """
    if mounts is not None:
        return _resolve(mounts)
    return _resolve_once()


def reset_cache_for_tests() -> None:
    """Forget the memoised answer, for tests that move the candidate paths."""
    _resolve_once.cache_clear()
