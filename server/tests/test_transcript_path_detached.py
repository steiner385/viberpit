"""Tests for Task 5: transcript_path resolves from the DB row for detached sessions."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _seed_and_rehydrate(db_url: str, session_id: str, cwd: str, claude_session_id: str | None):
    """Helper: seed a DB row and rehydrate the orchestrator from it."""
    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id,
                name="test-session",
                account_name="acct1",
                claude_session_id=claude_session_id,
                status="idle",
                cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch

    return asyncio.run(_run())


def test_transcript_path_detached_session_file_exists(tmp_path):
    """transcript_path returns the path when the file exists for a detached session."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    claude_sid = "claude-detached-1"
    session_id = "sess-detached-1"

    # Build the expected path structure:
    # <config_dir>/projects/<cwd_key>/<claude_session_id>.jsonl
    fake_config_dir = tmp_path / "fake-config"
    cwd_key = cwd.lstrip("/").replace("/", "-")
    transcript_dir = fake_config_dir / "projects" / cwd_key
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / f"{claude_sid}.jsonl"
    transcript_file.write_text('{"type":"text"}\n')

    orch = _seed_and_rehydrate(db_url, session_id, cwd, claude_sid)

    with patch("server.sessions.orchestrator.compose", return_value=fake_config_dir):
        result = orch.transcript_path(session_id)

    assert result == str(transcript_file)


def test_transcript_path_detached_session_file_missing(tmp_path):
    """transcript_path returns None when the file does NOT exist for a detached session."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    claude_sid = "claude-detached-2"
    session_id = "sess-detached-2"

    fake_config_dir = tmp_path / "fake-config2"

    orch = _seed_and_rehydrate(db_url, session_id, cwd, claude_sid)

    with patch("server.sessions.orchestrator.compose", return_value=fake_config_dir):
        result = orch.transcript_path(session_id)

    assert result is None
