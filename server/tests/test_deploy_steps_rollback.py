from types import SimpleNamespace

from server.deploy import steps


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
    assert ("restart", "viberpit-daemon.service") in seq
    assert ("restart", "viberpit.service") in seq
    # daemon restarted before edge
    assert seq.index(("restart", "viberpit-daemon.service")) < seq.index(("restart", "viberpit.service"))


def test_rollback_build_failure_does_not_raise():
    def run(cmd, **kw):
        rc = 1 if cmd[:2] == ["npm", "run"] else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="x")
    # must not raise even though build returns non-zero
    steps.rollback_to("/r", "lastgood", daemon_changed=False, daemon_enabled=True, run=run)


# Remote-runner rewind (production split-version, 2026-08-25: the health gate rejected
# f26c56ee, rollback reset dobby's edge to 0d15f128, and rocky's daemon was left on the
# rejected commit) is now owned by server.deploy.runners.rewind_runners
# (test_deploy_runners.py: test_rewind_only_moved_hosts_and_uses_reset_hard,
# test_remote_rewind_uses_reset_hard) and dispatched by the deploy controller — rollback_to
# itself is edge + LOCAL daemon only.
