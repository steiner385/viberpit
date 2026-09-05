import os
import types
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.api.sessions import _record_to_dict
from server.app import create_app
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def test_record_to_dict_includes_summary():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None, status="in_progress",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False, updated_at_iso=None, rev_at=None,
    )
    d = _record_to_dict(rec, summary="Refactoring auth")
    assert d["summary"] == "Refactoring auth"


def test_record_to_dict_summary_defaults_empty():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None, status="idle",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False, updated_at_iso=None, rev_at=None,
    )
    assert _record_to_dict(rec)["summary"] == ""


# ---------------------------------------------------------------------------
# Integration: wire contract — GET /api/sessions exercises the HTTP path
# through _record_to_dict, asserting "summary" appears in the real response.
# ---------------------------------------------------------------------------

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


def _make_fake_record():
    """Minimal record SimpleNamespace that satisfies _record_to_dict and _resolve_repo."""
    return types.SimpleNamespace(
        id="s1", name="Test Session", account="acct1", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None,
        status="idle", name_is_auto=True, detached=False, adopted_in_place=False,
        sub_agents=None, created_at=None, last_turn_at=None, account_pinned=False,
        model=None, active_goal=None, usage_unverified=False, updated_at_iso=None,
        rev_at=None,
        # cwd=None so _resolve_repo short-circuits to record.repo (None → "Scratch")
        cwd=None,
    )


class _FakeSessions:
    """Minimal facade for GET /api/sessions; exposes latest_summary_many as an AsyncMock.

    GET /api/sessions is wired through the batched _many RPCs (#742), so the fake must
    expose those — not the per-session latest_summary/latest_suggestions/prs_for, which
    only the version-skew fallback (_enrich_one) still calls.
    """

    def __init__(self, record, summary_return: str = ""):
        self._record = record
        self.latest_summary_many = AsyncMock(
            return_value={record.id: summary_return})

    async def list_sessions(self):
        return [self._record]

    async def latest_suggestions_many(self, sids, *, timeout=None):
        return {}

    async def prs_for_many(self, sids, *, timeout=None):
        return {}


def _build_client(summary_return: str = "Writing tests"):
    """Build app with a real orchestrator (for lifespan) then inject a fake facade."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(_make_fake_record(), summary_return=summary_return)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_get_sessions_wire_includes_summary():
    """GET /api/sessions?viewing=1 — response JSON contains 'summary' key with the expected value."""
    client, fake = _build_client(summary_return="Writing tests")
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/sessions?viewing=1", headers=_H)
    assert resp.status_code == 200, resp.text
    sessions = resp.json()
    assert len(sessions) == 1
    assert sessions[0]["summary"] == "Writing tests"


def test_get_sessions_viewing_flag_threaded():
    """?viewing=1 calls latest_summary(viewing=True); no param calls with viewing=False."""
    # viewing=True
    client_t, fake_t = _build_client(summary_return="Summary A")
    with patch.dict(os.environ, _ENV), client_t:
        resp = client_t.get("/api/sessions?viewing=1", headers=_H)
    assert resp.status_code == 200, resp.text
    fake_t.latest_summary_many.assert_awaited_once_with(["s1"], viewing=True)

    # viewing=False (no query param)
    client_f, fake_f = _build_client(summary_return="Summary B")
    with patch.dict(os.environ, _ENV), client_f:
        resp = client_f.get("/api/sessions", headers=_H)
    assert resp.status_code == 200, resp.text
    fake_f.latest_summary_many.assert_awaited_once_with(["s1"], viewing=False)
