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
