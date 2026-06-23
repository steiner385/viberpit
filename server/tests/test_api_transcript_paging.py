"""Tests for Task 3: paged transcript endpoint (?before=&limit=).

GET /api/sessions/{session_id}/transcript
  - no params: backward-compatible {"events": [...]} only
  - ?before=N&limit=M: {"events": [...], "earliestTurn": N, "totalTurns": N}
  - unknown session: 404
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.replay import ReplayWindow
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "apitest@example.com"}


def _write_transcript(tmp_path: Path, n_turns: int) -> str:
    """Write a JSONL transcript with n_turns user+assistant pairs."""
    p = tmp_path / "t.jsonl"
    lines = []
    for i in range(n_turns):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _make_app_with_patched_transcript(tmp_path: Path, n_turns: int, db_url: str):
    """Build a test app whose orchestrator's transcript resolution is patched."""
    transcript_path = _write_transcript(tmp_path, n_turns)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)

    # Patch _resolve_transcript_path (used by transcript_events + transcript_window)
    # to return the tmp file path for the seeded session, None for others.
    session_id = "test-session-paging-001"

    async def _fake_resolve(sid):
        return transcript_path if sid == session_id else None

    orch._resolve_transcript_path = _fake_resolve

    # Patch get_session so the session appears to exist (not 404)
    orch.get_session = AsyncMock(side_effect=lambda sid: (
        MagicMock(id=sid) if sid == session_id else None
    ))

    app = create_app(orchestrator=orch)
    return app, session_id


class TestTranscriptPaging:
    """HTTP-level tests for the paged transcript endpoint."""

    def test_transcript_paging_returns_window_and_meta(self, tmp_path):
        """?before=10&limit=5 returns earliestTurn, totalTurns, and 5 turns' events."""
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, sid = _make_app_with_patched_transcript(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sessions/{sid}/transcript?before=10&limit=5",
                headers=_IDENT,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["totalTurns"] == 25
        assert body["earliestTurn"] == 5   # turns 5..9
        starts = [e for e in body["events"] if e["type"] == "turnStart"]
        assert len(starts) == 5

    def test_transcript_no_params_is_backward_compatible(self, tmp_path):
        """No query params → response has only 'events' key (unchanged shape)."""
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, sid = _make_app_with_patched_transcript(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sessions/{sid}/transcript",
                headers=_IDENT,
            )
        assert r.status_code == 200, r.text
        assert set(r.json().keys()) == {"events"}

    def test_transcript_unknown_session_404(self, tmp_path):
        """Unknown session_id returns 404."""
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, _sid = _make_app_with_patched_transcript(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/sessions/does-not-exist/transcript?before=5",
                headers=_IDENT,
            )
        assert r.status_code == 404
