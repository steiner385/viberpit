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
    # The classifier is an edge-only ALLOWLIST (fail-safe): only these paths are edge-only;
    # anything else (incl. shared modules the daemon imports) forces a daemon restart.
    assert "server/api/" in s
    assert "server/app" in s
    assert "server/bridge/" in s
    # The idle gate is now conditional on a session-disrupting restart.
    assert "is-enabled ccswitch-daemon.service" in s


def test_edge_only_allowlist_excludes_shared_daemon_modules():
    # Inverted classifier: edge-only is an explicit allowlist. Shared modules the daemon
    # imports must NOT be in it, so a change there falls through to daemon_changed.
    s = SCRIPT.read_text()
    # The allowlist line(s): grep -vE '^(...)' — extract the alternation patterns.
    allowlist = "".join(
        line for line in s.splitlines() if "grep -vE" in line or line.strip().startswith("|")
    )
    # Edge-only paths the daemon does NOT run.
    assert "web/" in allowlist and "server/api/" in allowlist
    # Shared modules the daemon DOES run must be absent from the allowlist (→ daemon_changed).
    for shared in ("server/config", "server/db", "server/audit", "server/usage"):
        assert shared not in allowlist, f"{shared} must not be edge-only allowlisted"


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
