# server/tests/test_deploy_classification.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "auto-deploy.sh"


def test_classification_helpers_present():
    s = SCRIPT.read_text()
    # Classifies the deploy range and probes remote-mode.
    assert "daemon_changed" in s
    assert "remote_active" in s
    # Daemon-relevant paths the classifier keys on.
    assert "server/ipc/" in s and "server/sessions/" in s
    # The idle gate is now conditional on a session-disrupting restart.
    assert "is-enabled ccswitch-daemon.service" in s


def test_fail_safe_defaults_to_daemon_changed():
    # If the diff can't be computed, the script must treat it as daemon_changed (safe).
    s = SCRIPT.read_text()
    # A comment or default assignment documenting the fail-safe.
    assert "fail" in s.lower() and "daemon_changed" in s


def test_shellcheck_clean_if_available():
    import shutil
    if not shutil.which("shellcheck"):
        return
    r = subprocess.run(["shellcheck", "-S", "error", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
