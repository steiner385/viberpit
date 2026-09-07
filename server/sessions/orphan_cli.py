"""Boot-time reap of `claude` CLIs a previous daemon incarnation left running (#1379).

``ccswitch-daemon.service`` runs ``KillMode=mixed``, so a restart signals the main
process and lets the control group's children finish. In practice they do not
finish — the journal records the outcome on every deploy::

    systemd: ccswitch-daemon.service: Killing process 4041937 (claude) with signal SIGKILL.
    systemd: ccswitch-daemon.service: Unit process 4041937 (claude) remains running after unit stopped.

Those survivors keep editing files and can push, but their output goes nowhere the
UI can see and the freshly-booted daemon does not know they exist. When it then
resumes the same conversation, one session id ends up with two live CLIs — the
duplicate-owner half of #1379 that the per-session lease
(:mod:`server.sessions.session_lease`) cannot cover, because an orphan holds no
lease and answers to nobody.

The rule this module applies is the one the deploy runbook already states: *the
daemon owns every live `claude` process*. So at daemon start, an SDK-protocol
``claude`` that is not a descendant of THIS process is, by construction, a leftover
of the incarnation that just died. Three conditions must all hold before we signal
anything:

1. ``adopt.classify`` says ``"sdk"`` — the stream-json driver protocol ccswitch
   itself speaks. A human's interactive ``claude`` classifies as ``"session"`` and
   an internal pty-host/spare as ``"helper"``; neither is ever touched.
2. **Nothing alive owns it** — walking ``ppid`` up finds no live non-CLI ancestor,
   only pid 1 or a parent that has already exited. See :func:`_orphaned` for why
   this, and not "is not a descendant of me", is the predicate.
3. Its ``CLAUDE_CONFIG_DIR`` maps to a managed ccswitch account. An SDK claude
   belonging to something else on the box (a developer's own agent, another
   checkout's test run) is left strictly alone.

Discovery is pure given an injected process lister, so the classification is unit
tested without a real ``/proc``; only :func:`reap_orphans` signals anything, and it
reuses ``adopt.terminate_process_tree`` (SIGTERM, grace, SIGKILL) rather than
adding a second way to kill a CLI.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ccswitch import paths as _paths

from server.sessions.adopt import (
    ProcInfo,
    _map_account,
    _proc_lister,
    classify,
    terminate_process_tree,
)

log = logging.getLogger(__name__)

__all__ = ["OrphanCli", "discover_orphans", "reap_orphans", "reap_enabled"]

# Kill switch. Reaping is ON by default — a surviving CLI is an unattributable
# writer with commit access, which is strictly worse than a session that has to be
# resumed — but an operator debugging a live orphan needs a way to keep it.
_REAP_ENV = "CCSWITCH_REAP_ORPHAN_CLIS"


def reap_enabled() -> bool:
    return os.environ.get(_REAP_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class OrphanCli:
    pid: int
    account: str
    cwd: str | None
    resume: str | None   # the --resume conversation id, when the argv carries one


def _resume_arg(cmdline: list[str]) -> str | None:
    """The value of ``--resume`` in *cmdline* (``--resume=X`` form included)."""
    for i, part in enumerate(cmdline):
        if part == "--resume":
            return cmdline[i + 1] if i + 1 < len(cmdline) else None
        if part.startswith("--resume="):
            return part.split("=", 1)[1] or None
    return None


def _orphaned(pid: int, by_pid: dict[int, ProcInfo]) -> bool:
    """True when no LIVE process still owns *pid* — the reap predicate.

    The obvious rule, "not a descendant of this daemon", is too broad. Any other
    live ccswitch process on the box owns CLIs too — an edge running its own
    in-process orchestrator, a second daemon during a staged cutover — and a
    daemon restart must not machine-gun theirs. The rule that actually means
    "leftover" is the one systemd reports: its owner is *gone*, so it has been
    reparented to init (or its parent exited between the snapshot and this walk).

    A CLI whose parent is another CLI keeps walking, so a helper the orphan itself
    spawned is still reaped with it. A live NON-CLI ancestor — a daemon, an edge, a
    developer's shell — stops the walk and saves the process. The asymmetry is
    deliberate: a false negative leaves one orphan alive, which is the status quo
    and recoverable; a false positive kills someone's live session, which is not.

    Bounded by the pids seen: a ``/proc`` snapshot is not atomic, so a process can
    be reparented mid-walk and produce a cycle. Visiting each pid once terminates.
    """
    seen: set[int] = set()
    cur = by_pid.get(pid)
    while cur is not None:
        ppid = cur.ppid
        if ppid <= 1 or ppid in seen:
            return True          # reparented to init — the owner is gone
        seen.add(ppid)
        parent = by_pid.get(ppid)
        if parent is None:
            return True          # parent already exited
        if classify(parent.cmdline) != "sdk":
            return False         # a live owner is still responsible for it
        cur = parent             # a CLI owned by a CLI — keep walking up
    return True                  # unreachable: the loop only exits via a return


def discover_orphans(
    known_accounts: list[str],
    *,
    self_pid: int | None = None,
    proc_lister=_proc_lister,
    account_cfg=_paths.account_cfg_dir,
) -> list[OrphanCli]:
    """SDK `claude` processes on this host that nothing alive owns.

    See the module docstring for the three conditions. ``self_pid`` is excluded
    outright; every CLI *we* started is already saved by :func:`_orphaned` finding
    us alive on its parent chain. Returns an empty list rather than raising when
    ``/proc`` is unreadable — this runs on the daemon boot path, where a diagnostic
    sweep may never prevent a start.
    """
    me = os.getpid() if self_pid is None else self_pid
    try:
        procs: list[ProcInfo] = proc_lister()
    except Exception:
        log.warning("orphan-cli: process enumeration failed", exc_info=True)
        return []
    by_pid = {p.pid: p for p in procs}
    home_default = os.path.expanduser("~/.claude")
    out: list[OrphanCli] = []
    for proc in procs:
        if proc.pid == me or classify(proc.cmdline) != "sdk":
            continue
        if not _orphaned(proc.pid, by_pid):
            continue  # a live process is still responsible for it
        config_dir = proc.environ.get("CLAUDE_CONFIG_DIR", home_default)
        account = _map_account(config_dir, known_accounts, account_cfg)
        if account is None:
            continue  # not a ccswitch-managed CLI; not ours to kill
        out.append(OrphanCli(pid=proc.pid, account=account, cwd=proc.cwd,
                             resume=_resume_arg(proc.cmdline)))
    return out


def reap_orphans(
    known_accounts: list[str],
    *,
    self_pid: int | None = None,
    proc_lister=_proc_lister,
    account_cfg=_paths.account_cfg_dir,
    terminate=terminate_process_tree,
) -> list[OrphanCli]:
    """Terminate every orphan :func:`discover_orphans` finds; return those signalled.

    Each reap is logged at WARNING with the pid, account, cwd and the conversation
    id it was resuming — the duplicate-owner incident was only diagnosable because
    someone ran ``pgrep`` by hand, and this is the same evidence in the journal.
    """
    if not reap_enabled():
        log.info("orphan-cli: reap disabled by %s", _REAP_ENV)
        return []
    reaped: list[OrphanCli] = []
    for orphan in discover_orphans(known_accounts, self_pid=self_pid,
                                   proc_lister=proc_lister, account_cfg=account_cfg):
        log.warning(
            "orphan-cli: reaping leftover CLI pid=%d account=%s resume=%s cwd=%s — "
            "it survived a previous daemon and would be a second writer for this session",
            orphan.pid, orphan.account, orphan.resume or "<none>", orphan.cwd or "<unknown>",
        )
        try:
            if terminate(orphan.pid):
                reaped.append(orphan)
        except Exception:
            log.warning("orphan-cli: failed to terminate pid=%d", orphan.pid, exc_info=True)
    return reaped
