from server.deploy import steps


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:  # minimal stand-in for CompletedProcess
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


def test_remote_daemon_restarts_over_ssh(monkeypatch):
    """With a remote daemon host configured, the daemon restart must go over SSH."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("ssh tony@rocky") for c in joined), (
        f"expected an ssh daemon restart, got {joined}")
    assert any("ccswitch-daemon.service" in c for c in joined)
    assert "ccswitch-daemon.service" in units
    # the edge still restarts locally, and still AFTER the daemon (order is load-bearing, #190)
    assert joined[-1] == "sudo systemctl restart ccswitch.service"


def test_local_behavior_unchanged_without_the_env_var(monkeypatch):
    """Single-host deployments must behave exactly as before."""
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=True,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert joined == ["sudo systemctl restart ccswitch-daemon.service",
                      "sudo systemctl restart ccswitch.service"]
    assert units == ["ccswitch-daemon.service", "ccswitch.service"]


def test_no_stray_local_daemon_restart_in_remote_mode(monkeypatch):
    """In remote mode, only the ssh form of the daemon restart may run — never a bare local one."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=False, run=_recorder(calls))
    assert ["sudo", "systemctl", "restart", "ccswitch-daemon.service"] not in calls
