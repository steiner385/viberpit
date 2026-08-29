from types import SimpleNamespace

import pytest

from server.deploy import steps
from server.deploy.types import DeployError


def test_rollback_resets_then_restarts_both():
    seq = []
    def run(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and "reset" in cmd:
            seq.append(("reset", cmd[-1]))
        if "restart" in cmd:
            seq.append(("restart", cmd[-1]))
        if cmd[:2] == ["npm", "run"]:
            seq.append(("build", None))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.rollback_to("/r", "lastgood", daemon_changed=True, daemon_enabled=True, run=run)
    assert seq[0] == ("reset", "lastgood")
    assert ("restart", "ccswitch-daemon.service") in seq
    assert ("restart", "ccswitch.service") in seq
    # daemon restarted before edge
    assert seq.index(("restart", "ccswitch-daemon.service")) < seq.index(("restart", "ccswitch.service"))


def test_rollback_build_failure_does_not_raise():
    def run(cmd, **kw):
        rc = 1 if cmd[:2] == ["npm", "run"] else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="x")
    # must not raise even though build returns non-zero
    steps.rollback_to("/r", "lastgood", daemon_changed=False, daemon_enabled=True, run=run)


# --- Remote rewind (production split-version, 2026-08-25) -----------------------
#
# rollback_to's docstring promises "Restart BOTH units together so they run the same
# (old) code -- never split-version". It called restart_units WITHOUT target_sha, and
# restart_units falls back to `merge --ff-only origin/master` -- fast-forwarding the
# remote daemon to the very commit being rolled back. It also cannot work in principle:
# --ff-only can never move a branch backward.
#
# Live consequence: the health gate rejected f26c56ee, rollback reset dobby's edge to
# 0d15f128, and rocky's daemon was left on the rejected commit.

def _remote_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")


def _recorder():
    cmds = []
    def run(cmd, **kw):
        cmds.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return cmds, run


def test_rollback_rewinds_the_remote_daemon_to_the_good_sha(monkeypatch):
    """The remote must land on the SAME commit the edge was just reset to."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.rollback_to("/r", "0d15f128", daemon_changed=True, daemon_enabled=True, run=run)
    assert ["ssh", "tony@rocky", "git", "-C", "/home/tony/GitHub/ccswitch",
            "reset", "--hard", "0d15f128"] in cmds


def test_rollback_never_fast_forwards_the_remote(monkeypatch):
    """--ff-only cannot move backward, and its `or origin/master` fallback aims the
    remote at exactly the commit being rolled back."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.rollback_to("/r", "0d15f128", daemon_changed=True, daemon_enabled=True, run=run)
    assert not any("merge" in c for c in cmds if c[:1] == ["ssh"])
    assert not any("origin/master" in c for c in cmds)


def test_rollback_restarts_the_remote_daemon_after_rewinding_it(monkeypatch):
    """Rewinding the checkout without restarting leaves the rejected code running."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.rollback_to("/r", "0d15f128", daemon_changed=True, daemon_enabled=True, run=run)
    reset_at = next(i for i, c in enumerate(cmds) if "reset" in c and c[:1] == ["ssh"])
    restart_at = next(i for i, c in enumerate(cmds)
                      if c[:1] == ["ssh"] and "ccswitch-daemon.service" in c)
    assert reset_at < restart_at


def test_a_rewind_without_a_target_sha_is_refused(monkeypatch):
    """Rewinding to "wherever origin points" is the bug, not a fallback. Fail loud."""
    _remote_env(monkeypatch)
    _, run = _recorder()
    with pytest.raises(DeployError):
        steps.restart_units(daemon_changed=True, daemon_enabled=True,
                            target_sha=None, rewind_remote=True, run=run)


def test_a_forward_deploy_still_fast_forwards_the_remote(monkeypatch):
    """The rewind path must not leak into normal deploys: a diverged remote should
    still abort the deploy rather than being silently reset."""
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    steps.restart_units(daemon_changed=True, daemon_enabled=True,
                        target_sha="f26c56ee", run=run)
    assert ["ssh", "tony@rocky", "git", "-C", "/home/tony/GitHub/ccswitch",
            "merge", "--ff-only", "f26c56ee"] in cmds
    assert not any("reset" in c for c in cmds)


def test_rollback_accepts_the_full_sha_the_controller_actually_passes(monkeypatch):
    """Coupling guard. The controller rolls back to `git.current_sha` (`rev-parse @`,
    a full 40-char sha), and the rewind path now VALIDATES that value -- so a
    non-hex sha would turn a rollback into a raise, losing the rollback entirely.
    Pin the shape the caller really supplies.
    """
    _remote_env(monkeypatch)
    cmds, run = _recorder()
    full = "0d15f1280d8b1b9f296e60939c21ccd63935da63"
    steps.rollback_to("/r", full, daemon_changed=True, daemon_enabled=True, run=run)
    assert ["ssh", "tony@rocky", "git", "-C", "/home/tony/GitHub/ccswitch",
            "reset", "--hard", full] in cmds
