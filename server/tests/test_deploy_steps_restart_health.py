from types import SimpleNamespace
from server.deploy import steps


def test_daemon_restarts_before_edge():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    units = steps.restart_units(daemon_changed=True, daemon_enabled=True, run=run)
    assert units == ["ccswitch-daemon.service", "ccswitch.service"]
    assert order == ["ccswitch-daemon.service", "ccswitch.service"]


def test_edge_only_restarts_edge_alone():
    units = steps.restart_units(daemon_changed=False, daemon_enabled=True,
                                run=lambda c, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert units == ["ccswitch.service"]


def test_daemon_disabled_skips_daemon_restart():
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=lambda c, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert units == ["ccswitch.service"]


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


def test_remote_daemon_configured_reads_ssh_host(monkeypatch):
    """A REMOTE daemon is present iff CCSWITCH_DAEMON_SSH_HOST is set — the same env var
    restart_units uses to decide whether the daemon restart is issued over ssh. One source of
    truth so the health window and the restart path never disagree about the topology."""
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    assert steps.remote_daemon_configured() is False
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "rocky")
    assert steps.remote_daemon_configured() is True
