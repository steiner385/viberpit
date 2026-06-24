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
