from pathlib import Path
from types import SimpleNamespace

from server.deploy import steps

ROOT = Path(__file__).resolve().parents[2]


def test_daemon_unit_has_required_keys():
    unit = (ROOT / "deploy" / "ccswitch-daemon.service").read_text()
    assert "RuntimeDirectory=ccswitch" in unit
    assert "ExecStart=" in unit and "server.ipc" in unit
    assert "EnvironmentFile=/etc/ccswitch/ccswitch.env" in unit
    assert "WantedBy=multi-user.target" in unit


def test_daemon_restarts_before_edge_when_daemon_changed():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.restart_units(daemon_changed=True, daemon_enabled=True, run=run)
    assert order == ["ccswitch-daemon.service", "ccswitch.service"]


def test_daemon_skipped_when_not_enabled():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.restart_units(daemon_changed=True, daemon_enabled=False, run=run)
    assert order == ["ccswitch.service"]
