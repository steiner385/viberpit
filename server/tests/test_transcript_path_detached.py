"""Tests for Task 5: transcript_path resolves from the DB row for detached sessions."""
from __future__ import annotations

import asyncio
from unittest.mock import patch


from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _seed_and_rehydrate(db_url: str, session_id: str, cwd: str, engine_session_id: str | None):
    """Helper: seed a DB row and rehydrate the orchestrator from it."""
    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id,
                name="test-session",
                account_name="acct1",
                engine_session_id=engine_session_id,
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
    # <config_dir>/projects/<cwd_key>/<engine_session_id>.jsonl
    fake_config_dir = tmp_path / "fake-config"
    cwd_key = cwd.lstrip("/").replace("/", "-")
    transcript_dir = fake_config_dir / "projects" / cwd_key
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / f"{claude_sid}.jsonl"
    transcript_file.write_text('{"type":"text"}\n')

    orch = _seed_and_rehydrate(db_url, session_id, cwd, claude_sid)

    with patch("server.sessions.orchestrator.account_cfg_dir", return_value=fake_config_dir):
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

    with patch("server.sessions.orchestrator.account_cfg_dir", return_value=fake_config_dir):
        result = orch.transcript_path(session_id)

    assert result is None


def test_transcript_path_found_under_nonmatching_project_dir(tmp_path):
    """Regression: the transcript is located by globbing the unique engine_session_id,
    so it resolves even when the project-dir name doesn't equal cwd.lstrip('/').replace('/','-').

    Claude's real convention keeps the leading slash (→ leading dash) and replaces '.'
    too, so the old hand-computed key never matched → blank history for every session.
    A cwd/project mismatch (session recorded a different cwd than it ran in) is also
    handled, since the UUID is globally unique across project dirs."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = "/mnt/ssk-ssd/tony/GitHub/cc-account-switcher"
    claude_sid = "uuid-unique-regression"
    session_id = "sess-leading-dash"

    fake_config_dir = tmp_path / "cfg"
    # Claude's REAL leading-dash convention (and an intentionally *different* project
    # dir than cwd, to also cover the cwd-mismatch case):
    real_project_dir = fake_config_dir / "projects" / "-mnt-ssk-ssd-tony-GitHub-pr-dashboard"
    real_project_dir.mkdir(parents=True)
    transcript_file = real_project_dir / f"{claude_sid}.jsonl"
    transcript_file.write_text('{"type":"text"}\n')

    orch = _seed_and_rehydrate(db_url, session_id, cwd, claude_sid)
    with patch("server.sessions.orchestrator.account_cfg_dir", return_value=fake_config_dir):
        result = orch.transcript_path(session_id)

    assert result == str(transcript_file), (
        "transcript_path must find the transcript by its unique session id regardless "
        "of the project-dir naming or a cwd/project mismatch"
    )


def test_transcript_path_resolves_via_shared_base_when_account_symlink_broken(tmp_path):
    """Regression (blank "Waiting for input" sessions): every account's per-account
    ``projects`` dir is a symlink to the one shared projects tree, but that symlink can
    rot — point at itself or a sibling account — which silently breaks replay on attach
    and leaves the transcript blank. The transcript physically lives in the shared base,
    so transcript_path must resolve by the globally-unique engine_session_id there even
    when the per-account symlink is broken.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = "/mnt/ssk-ssd/tony/GitHub/cc-account-switcher"
    claude_sid = "uuid-shared-base-regression"
    session_id = "sess-broken-symlink"

    # The transcript physically lives in the shared base's projects tree.
    shared = tmp_path / "shared-claude"
    shared_projects = shared / "projects" / "-mnt-ssk-ssd-tony-GitHub-cc-account-switcher"
    shared_projects.mkdir(parents=True)
    transcript_file = shared_projects / f"{claude_sid}.jsonl"
    transcript_file.write_text('{"type":"text"}\n')

    # The per-account config dir's ``projects`` is a BROKEN symlink (self-referential),
    # mirroring the observed corruption — glob through it finds nothing.
    account_cfg = tmp_path / "cfg" / "secondary"
    account_cfg.mkdir(parents=True)
    broken_link = account_cfg / "projects"
    broken_link.symlink_to(broken_link)  # points at itself → unresolvable
    assert not broken_link.exists()

    orch = _seed_and_rehydrate(db_url, session_id, cwd, claude_sid)
    with patch("server.sessions.orchestrator.account_cfg_dir", return_value=account_cfg), \
         patch("server.sessions.orchestrator.shared_base", return_value=shared):
        result = orch.transcript_path(session_id)

    assert result == str(transcript_file), (
        "transcript_path must fall back to the shared projects base (the physical home "
        "of every transcript) when the per-account projects symlink is broken"
    )
