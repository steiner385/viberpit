"""GET /api/sessions must return 200 with empty auxiliary fields when the daemon
is temporarily unavailable for suggestions, PRs, or summaries — a transient
ReadError must not 500 the whole sessions list.

Regression guard for the 2026-07-10 incident where httpx.ReadError from
latest_suggestions propagated through asyncio.gather as an unhandled exception."""
import os
import types
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.ipc.client import DaemonUnavailable
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


def _make_fake_record():
    return types.SimpleNamespace(
        id="s1", name="Test Session", account="acct1", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None,
        status="idle", name_is_auto=True, detached=False, adopted_in_place=False,
        sub_agents=None, created_at=None, account_pinned=False,
        model=None, active_goal=None, usage_unverified=False,
        updated_at_iso=None, rev_at=None, cwd=None,
    )


class _BlippedSessions:
    """Fake orchestrator facade whose batched auxiliary RPCs raise DaemonUnavailable.

    GET /api/sessions is wired through _enrich_batch's three batched _many RPCs
    (#742), not the per-session latest_summary/latest_suggestions/prs_for — those are
    only reached by the version-skew fallback (_enrich_one). A fake exposing only the
    per-session methods is never invoked at all: _enrich_batch's ``getattr(orch, name,
    None) is None`` guard silently degrades to ``{}`` without ever calling anything,
    which made this regression guard pass green while testing nothing.
    """

    def __init__(self, record):
        self._record = record
        self.latest_summary_many = AsyncMock(side_effect=DaemonUnavailable("blip"))

    async def list_sessions(self):
        return [self._record]

    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        raise DaemonUnavailable("blip")

    async def prs_for_many(self, session_ids, *, timeout=None):
        raise DaemonUnavailable("blip")


def _build_blipped_client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    app.state.sessions = _BlippedSessions(_make_fake_record())
    return TestClient(app, raise_server_exceptions=False)


def test_get_sessions_returns_200_when_suggestions_unavailable():
    """Auxiliary RPC failure (suggestions/prs/summaries) must not 500 the list."""
    with patch.dict(os.environ, _ENV), _build_blipped_client() as client:
        resp = client.get("/api/sessions", headers=_H)
    assert resp.status_code == 200, resp.text
    sessions = resp.json()
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["latestSuggestions"] == []
    assert sess["prs"] == []
    assert sess["summary"] == ""
