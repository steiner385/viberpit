# server/tests/test_deploy_classification.py
from server.deploy import steps


def test_edge_only_paths_classify_edge_only():
    files = ["web/src/App.tsx", "server/api/x.py", "server/app.py",
             "server/bridge/ws.py", "docs/y.md", ".github/workflows/ci.yml"]
    assert steps.classify_change(files) == "edge_only"


def test_shared_daemon_modules_force_daemon_changed():
    for shared in ("server/config.py", "server/db/models.py",
                   "server/audit.py", "server/usage.py",
                   "server/sessions/orchestrator.py", "server/ipc/daemon.py"):
        assert steps.classify_change([shared]) == "daemon_changed", shared


def test_uncomputable_diff_fails_safe():
    assert steps.classify_change(None) == "daemon_changed"
