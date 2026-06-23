"""Tests for Task 4: transcript export endpoints + formatter.

Covers:
  - transcript_markdown() unit tests (formatter)
  - GET /api/sessions/{session_id}/export.md   → text/markdown, attachment
  - GET /api/sessions/{session_id}/export.jsonl → application/x-ndjson, attachment, raw
  - 404 when no transcript is available
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.sessions.export import transcript_markdown


# ---------------------------------------------------------------------------
# Formatter unit tests
# ---------------------------------------------------------------------------


def test_markdown_renders_roles_and_tools():
    events = [
        {"type": "sessionMeta", "model": "claude-x", "sessionId": "s1"},
        {"type": "user", "turnId": "u0", "text": "hello", "images": []},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "textDelta", "turnId": "a0", "text": "hi there"},
        {"type": "thinkingDelta", "turnId": "a0", "text": "ponder"},
        {"type": "toolCall", "turnId": "a0", "toolId": "t1", "name": "Bash",
         "toolKind": "bash", "input": {"command": "ls"}},
        {"type": "toolResult", "toolId": "t1", "ok": True, "payload": {"content": "file.txt"}},
        {"type": "turnEnd", "turnId": "a0", "usage": None},
    ]
    md = transcript_markdown(events, title="My Session")
    assert "# My Session" in md
    assert "## User" in md and "hello" in md
    assert "## Assistant" in md and "hi there" in md
    assert "Bash" in md and "ls" in md          # tool call rendered
    assert "file.txt" in md                      # tool result rendered
    assert "claude-x" in md                      # model in header


def test_markdown_empty_is_safe():
    assert isinstance(transcript_markdown([], title="Empty"), str)


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

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


def _make_app(tmp_path: Path, n_turns: int, db_url: str, *, has_transcript: bool = True):
    """Build a test app with a patched orchestrator for export tests."""
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    transcript_path = _write_transcript(tmp_path, n_turns) if has_transcript else None
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)

    session_id = "test-session-export-001"
    no_transcript_id = "test-session-no-transcript"

    async def _fake_transcript_path(sid):
        if sid == session_id and has_transcript:
            return transcript_path
        return None

    # Patch transcript_path and get_session on the orchestrator
    orch._resolve_transcript_path = _fake_transcript_path
    # transcript_path is the sync method on orchestrator; we also need to patch
    # the method on the facade (which wraps the orchestrator). Easiest: patch
    # transcript_path directly on the orchestrator instance so the facade forwards it.
    orch.transcript_path = lambda sid: (
        transcript_path if (sid == session_id and has_transcript) else None
    )

    orch.get_session = AsyncMock(side_effect=lambda sid: (
        MagicMock(id=sid, name="Test Session", title=None) if sid in (session_id, no_transcript_id) else None
    ))

    # transcript_events: return a small list of mock event objects
    def _make_event(i):
        e = MagicMock()
        e.to_wire.return_value = {
            "type": "user",
            "turnId": f"u{i}",
            "text": f"q{i}",
            "images": [],
        }
        return e

    orch.transcript_events = AsyncMock(
        return_value=[_make_event(i) for i in range(n_turns)]
    )

    app = create_app(orchestrator=orch)
    return app, session_id, no_transcript_id


class TestExportEndpoints:
    """HTTP-level tests for the transcript export endpoints."""

    def test_export_md_download(self, tmp_path):
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, sid, _ = _make_app(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(f"/api/sessions/{sid}/export.md", headers=_IDENT)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/markdown")
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["content-disposition"].endswith('.md"')

    def test_export_jsonl_raw_download(self, tmp_path):
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, sid, _ = _make_app(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(f"/api/sessions/{sid}/export.jsonl", headers=_IDENT)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in r.headers["content-disposition"]
        # raw passthrough: first non-empty line parses as JSON
        first = next(l for l in r.text.splitlines() if l.strip())
        json.loads(first)

    def test_export_missing_transcript_404(self, tmp_path):
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
        app, _, no_transcript_id = _make_app(tmp_path, 25, db_url)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                f"/api/sessions/{no_transcript_id}/export.md",
                headers=_IDENT,
            )
        assert r.status_code == 404
