"""A deploy must flush the checkout to disk before restarting into it.

ext4 mounts with delayed allocation by default (rocky: `ext4 rw,relatime`). A
just-written file's SIZE is journaled long before its data blocks are allocated,
so a host that dies inside that window brings the file back as ZERO BYTES.

Production, 2026-08-29:

    21:29:42  deploy's `git merge --ff-only` rewrote six files on rocky
    21:30:02  rocky hard-crashed (journal ends mid-line, no shutdown sequence)
    21:31:53  boot; ext4 replay -> those six files are 0 bytes
              server/sessions/orchestrator.py: 7,864 lines -> empty

An empty module still imports, so the daemon failed with `ImportError: cannot
import name 'SessionOrchestrator'` and crash-looped 57 times. systemd's
Restart=always can NEVER recover that: every restart re-reads the same empty
file. The health watchdog saw the daemon unreachable and deliberately stood down
("systemd Restart=always on the daemon host owns recovery there"), so nothing
escalated for ~15 minutes.

`sync` after the git write closes the window.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.deploy import steps


def _remote_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")


def _recorder():
    cmds = []
    def run(cmd, **kw):
        cmds.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return cmds, run


def _idx(cmds, pred) -> int:
    return next(i for i, c in enumerate(cmds) if pred(c))


def test_remote_checkout_is_flushed_before_the_daemon_restarts(monkeypatch):
    """The whole point: no restart into a checkout that is still only in page cache."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.restart_units(daemon_changed=True, daemon_enabled=True,
                        target_sha="abc1234", run=run)
    sync_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "sync" in c)
    write_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "merge" in c)
    restart_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "ccswitch-daemon.service" in c)
    assert write_at < sync_at < restart_at, (
        "sync must land AFTER the git write and BEFORE the restart; "
        f"got write={write_at} sync={sync_at} restart={restart_at}")


def test_a_rollback_rewind_is_flushed_too(monkeypatch):
    """The rewind path writes the same files and carries the same hazard."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.restart_units(daemon_changed=True, daemon_enabled=True,
                        target_sha="abc1234", rewind_remote=True, run=run)
    sync_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "sync" in c)
    write_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "reset" in c)
    restart_at = _idx(cmds, lambda c: c[:1] == ["ssh"] and "ccswitch-daemon.service" in c)
    assert write_at < sync_at < restart_at


def test_the_sync_targets_the_remote_not_the_deploy_host(monkeypatch):
    """A local sync would flush dobby's page cache and leave rocky's dirty."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.restart_units(daemon_changed=True, daemon_enabled=True,
                        target_sha="abc1234", run=run)
    syncs = [c for c in cmds if "sync" in c]
    assert ["ssh", "tony@rocky", "sync"] in syncs


def test_no_remote_sync_when_the_daemon_was_not_updated(monkeypatch):
    """daemon_changed=False writes nothing remotely, so there is nothing to flush."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.restart_units(daemon_changed=False, daemon_enabled=True,
                        target_sha="abc1234", run=run)
    assert not any(c[:1] == ["ssh"] for c in cmds)


def test_a_failing_sync_aborts_the_deploy(monkeypatch):
    """Restarting into an unflushed checkout is the failure this exists to prevent,
    so a sync that does not succeed must stop the deploy, not be shrugged off."""
    _remote_env(monkeypatch)
    def run(cmd, **kw):
        if "sync" in cmd:
            raise RuntimeError("sync failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(RuntimeError):
        steps.restart_units(daemon_changed=True, daemon_enabled=True,
                            target_sha="abc1234", run=run)
