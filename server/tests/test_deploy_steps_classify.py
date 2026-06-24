from server.deploy import steps


def test_all_edge_files_is_edge_only():
    assert steps.classify_change(["web/src/App.tsx", "server/api/x.py", "docs/y.md"]) == "edge_only"


def test_shared_daemon_module_forces_daemon_changed():
    assert steps.classify_change(["web/src/App.tsx", "server/config.py"]) == "daemon_changed"


def test_orchestrator_change_is_daemon_changed():
    assert steps.classify_change(["server/sessions/orchestrator.py"]) == "daemon_changed"


def test_none_diff_fails_safe_to_daemon_changed():
    assert steps.classify_change(None) == "daemon_changed"


def test_app_py_is_edge_only_but_db_is_not():
    assert steps.classify_change(["server/app.py"]) == "edge_only"
    assert steps.classify_change(["server/db/models.py"]) == "daemon_changed"
