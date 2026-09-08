from types import SimpleNamespace
from server.deploy import steps


def test_restart_edge_restarts_only_the_edge_unit(monkeypatch):
    """restart_edge is edge-only now — runner (daemon) restarts, local and remote, are owned
    by server.deploy.runners and run BEFORE it. The env var that used to steer restart_units'
    daemon branch must have no effect here at all."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    order = []
    def run(cmd, **kw):
        if "show" in cmd:
            return SimpleNamespace(returncode=0, stdout="LoadState=loaded\n")
        order.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    units = steps.restart_edge(run=run)
    assert units == ["viberpit.service"]
    assert order == [["sudo", "systemctl", "restart", "viberpit.service"]]


def test_health_requires_both_status_and_sha():
    bodies = iter([
        '{"status":"ok","sha":"deadbee"}',     # right status, WRONG sha → keep waiting
        '{"status":"ok","sha":"abc1234"}',     # match
    ])
    ok = steps.wait_healthy("abc1234", url="http://h/healthz", attempts=5,
                            fetch=lambda u: next(bodies), sleep=lambda s: None)
    assert ok is True


def test_health_gives_up_after_attempts():
    ok = steps.wait_healthy("abc1234", url="http://h/healthz", attempts=3,
                            fetch=lambda u: '{"status":"ok","sha":"other00"}',
                            sleep=lambda s: None)
    assert ok is False


def test_gate_fetch_timeout_outlasts_a_slow_but_healthy_healthz(monkeypatch):
    """The gate must poll /healthz with a read timeout longer than a healthy /healthz can take.

    In remote mode /healthz makes daemon RPCs (active_session_count, x11_metrics); just after a
    restart the daemon can answer slowly while it warms up and the edge re-attaches over the
    tailnet. A 3s poll timeout gave up before a slow-but-healthy /healthz answered, so the gate
    saw only empty bodies and rolled back a healthy deploy (#780). The timeout must comfortably
    exceed that warmup latency.
    """
    import httpx

    captured: dict = {}

    def fake_get(url, timeout):
        captured["timeout"] = timeout

        class _R:
            text = ""

        return _R()

    monkeypatch.setattr(httpx, "get", fake_get)
    steps._default_fetch("http://h/healthz")
    assert captured["timeout"] >= 6.0


def test_remote_runners_configured_reads_the_fleet(monkeypatch):
    """A REMOTE runner is present iff the fleet has an enabled host with an ssh_host — the
    registry, not an env var, is the one source of truth for topology now."""
    from server.deploy.runners import RunnerTarget
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    local = RunnerTarget(name="dobby", endpoint="https://d", enabled=True, healthy=True,
                         ssh_host=None, repo_path=None)
    remote = RunnerTarget(name="rocky", endpoint="https://r", enabled=True, healthy=True,
                          ssh_host="rocky", repo_path="/srv/cc")
    assert steps.remote_runners_configured(None) is False
    assert steps.remote_runners_configured([local]) is False
    assert steps.remote_runners_configured([local, remote]) is True
