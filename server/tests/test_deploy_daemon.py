# server/tests/test_deploy_daemon.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daemon_unit_has_required_keys():
    unit = (ROOT / "deploy" / "ccswitch-daemon.service").read_text()
    assert "RuntimeDirectory=ccswitch" in unit
    assert "ExecStart=" in unit and "server.ipc" in unit
    assert "EnvironmentFile=/etc/ccswitch/ccswitch.env" in unit
    assert "WantedBy=multi-user.target" in unit


def test_auto_deploy_guards_daemon_restart_behind_is_enabled():
    script = (ROOT / "scripts" / "auto-deploy.sh").read_text()
    # The daemon restart must be guarded so a host without the unit is unaffected.
    assert "ccswitch-daemon.service" in script
    assert "is-enabled" in script


def test_auto_deploy_shellcheck_clean():
    # Skip if shellcheck isn't installed; otherwise it must pass.
    import shutil
    if not shutil.which("shellcheck"):
        return
    r = subprocess.run(["shellcheck", "-S", "error", str(ROOT / "scripts" / "auto-deploy.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
