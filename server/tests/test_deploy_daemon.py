from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daemon_unit_has_required_keys():
    unit = (ROOT / "deploy" / "viberpit-daemon.service").read_text()
    assert "RuntimeDirectory=viberpit" in unit
    assert "ExecStart=" in unit and "server.ipc" in unit
    assert "EnvironmentFile=/etc/viberpit/viberpit.env" in unit
    assert "WantedBy=multi-user.target" in unit


# Daemon-before-edge ordering (local AND remote) now lives in
# server.deploy.runners (test_deploy_runners.py) and the controller ordering test —
# steps.restart_edge is edge-only and has no daemon branch to order against.
