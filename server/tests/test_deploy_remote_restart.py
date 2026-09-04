"""restart_edge: the edge restart is LOCAL and edge-only. Runner restarts live in
server.deploy.runners (test_deploy_runners.py) — the ssh branch that used to live here
(CCSWITCH_DAEMON_SSH_HOST) is gone, and steps must never build an ssh command again."""
from server.deploy import steps


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


def test_restart_edge_restarts_only_the_edge_unit(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")   # must be ignored now
    calls: list[list[str]] = []
    units = steps.restart_edge(run=_recorder(calls))
    assert calls == [["sudo", "systemctl", "restart", "ccswitch.service"]]
    assert units == ["ccswitch.service"]


def test_steps_has_no_restart_units_anymore():
    assert not hasattr(steps, "restart_units")


def test_rollback_to_resets_edge_and_restarts_local_daemon_when_changed(tmp_path):
    calls: list[list[str]] = []
    steps.rollback_to(str(tmp_path), "abc1234", daemon_changed=True, daemon_enabled=True,
                      run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert joined[0] == f"git -C {tmp_path} reset --hard abc1234"
    assert "sudo systemctl restart ccswitch-daemon.service" in joined
    assert joined[-1] == "sudo systemctl restart ccswitch.service"
    assert not any(j.startswith("ssh") for j in joined)


def test_remote_runners_configured_reads_the_fleet_not_the_env(monkeypatch):
    from server.deploy.runners import RunnerTarget
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    local = RunnerTarget(name="dobby", endpoint="https://d", enabled=True, healthy=True,
                         ssh_host=None, repo_path=None)
    remote = RunnerTarget(name="rocky", endpoint="https://r", enabled=True, healthy=True,
                          ssh_host="rocky", repo_path="/srv/cc")
    assert steps.remote_runners_configured([local]) is False
    assert steps.remote_runners_configured([local, remote]) is True
    assert steps.remote_runners_configured([]) is False
