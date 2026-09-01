"""Regression test: PM onboarding RPC handlers must return JSON-serializable values.

Before the fix, ``pm_onboarding_counts`` and ``pm_onboard_spawn_map`` were wired
through ``_SIMPLE`` which returned the raw orchestrator return value directly to the
transport's ``json.dumps``. ``pm_onboarding_counts`` returns a ``ProjectOnboardingView``
frozen dataclass (not JSON-native) and ``pm_onboard_spawn_map`` returns a session-record
object — both raise ``TypeError: Object of type X is not JSON serializable`` in remote
mode, causing 500s on the live daemon.

The fix:
- Removes both from ``_SIMPLE`` and adds custom handlers that convert first.
- ``pm_onboarding_counts`` → ``dataclasses.asdict(view)``
- ``pm_onboard_spawn_map`` → ``{"id": getattr(rec, "id", None)}`` (or ``None``)
- ``pm_classify_run`` stays in ``_SIMPLE`` (returns int — JSON-native).
"""
import json
from unittest import mock

from fastapi.testclient import TestClient

from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.pm.onboarding import ProjectOnboardingView
from server.sessions.orchestrator import SessionOrchestrator


def _make_view(**overrides) -> ProjectOnboardingView:
    defaults = dict(
        state="syncing",
        repo_count=2,
        backfill_done_count=1,
        feature_count=0,
        open_unclassified_count=0,
        pending_proposal_count=0,
        map_pr_open=False,
        map_pr_merged=False,
    )
    defaults.update(overrides)
    return ProjectOnboardingView(**defaults)


class _FakeRec:
    """Minimal object with an .id attribute — mirrors a real SessionRecord."""
    def __init__(self, session_id: str):
        self.id = session_id


def _rpc_app():
    app = create_daemon_app()
    orch = SessionOrchestrator(
        sdk_factory=mock.MagicMock(),
        poll_interval=9999.0,
        usage_fn=lambda n: None,
    )
    register_orchestrator_rpc(app, orch)
    return app, orch


# ---------------------------------------------------------------------------
# pm_onboarding_counts — dataclass → must round-trip through json.dumps
# ---------------------------------------------------------------------------

def test_pm_onboarding_counts_rpc_is_json_serializable():
    """Handler must return a plain dict (dataclasses.asdict), not the dataclass itself."""
    app, orch = _rpc_app()
    view = _make_view(repo_count=3, backfill_done_count=2)
    orch.pm_onboarding_counts = mock.AsyncMock(return_value=view)

    with TestClient(app) as c:
        resp = c.post("/rpc/pm_onboarding_counts", json={"project_id": "proj-1"})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    # Must be JSON-serializable at the transport level — TestClient already proved it.
    # Double-check explicitly so the test fails early with a clear message if serialization breaks.
    json.dumps(result)  # raises TypeError if dataclass slipped through
    # Shape: asdict of ProjectOnboardingView
    assert result["state"] == "syncing"
    assert result["repo_count"] == 3
    assert result["backfill_done_count"] == 2
    assert result["feature_count"] == 0
    assert result["map_pr_open"] is False
    assert result["map_pr_merged"] is False
    orch.pm_onboarding_counts.assert_awaited_once_with(project_id="proj-1")


# ---------------------------------------------------------------------------
# pm_onboard_spawn_map — object → must strip to {"id": ...}
# ---------------------------------------------------------------------------

def test_pm_onboard_spawn_map_rpc_is_json_serializable_with_record():
    """Handler must return {"id": <str>} when the orchestrator returns an object."""
    app, orch = _rpc_app()
    orch.pm_onboard_spawn_map = mock.AsyncMock(return_value=_FakeRec("session-abc"))

    with TestClient(app) as c:
        resp = c.post("/rpc/pm_onboard_spawn_map", json={"slug": "my-project"})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    json.dumps(result)  # must not raise
    assert result == {"id": "session-abc"}
    orch.pm_onboard_spawn_map.assert_awaited_once_with(slug="my-project")


def test_pm_onboard_spawn_map_rpc_returns_none_when_orchestrator_returns_none():
    """Handler must propagate None (no capacity) as JSON null."""
    app, orch = _rpc_app()
    orch.pm_onboard_spawn_map = mock.AsyncMock(return_value=None)

    with TestClient(app) as c:
        resp = c.post("/rpc/pm_onboard_spawn_map", json={"slug": "my-project"})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    json.dumps(result)  # must not raise
    assert result is None


# ---------------------------------------------------------------------------
# pm_classify_run — int → must stay JSON-serializable (kept in _SIMPLE)
# ---------------------------------------------------------------------------

def test_pm_classify_run_rpc_is_json_serializable():
    """pm_classify_run returns int — stays in _SIMPLE and must still round-trip."""
    app, orch = _rpc_app()
    orch.pm_classify_run = mock.MagicMock(return_value=5)

    with TestClient(app) as c:
        resp = c.post("/rpc/pm_classify_run", json={"slug": "my-project"})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    json.dumps(result)  # must not raise
    assert result == 5
    orch.pm_classify_run.assert_called_once_with(slug="my-project")
