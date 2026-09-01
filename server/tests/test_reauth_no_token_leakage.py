"""Test that captured tokens never leak into logs or audit records (Task 7).

Covers:
  - capture_login runs through the fake without logging token material
  - sweep_stale removes aged dirs and keeps fresh ones
  - audit_events.detail never contains accessToken/refreshToken/subscriptionType
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure src is importable when running from the repo root
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch import auth_login, paths
from server.reauth import ReauthManager


FAKE_CLAUDE = str(Path(__file__).parent / "fakes" / "fake_claude_login.py")
FAKE_AT = "fake-at"
FAKE_RT = "fake-rt"


@pytest.mark.asyncio
async def test_capture_login_no_token_leakage_in_logs(tmp_path, monkeypatch, caplog):
    """Drive capture_login through the fake; assert no tokens in any log message.

    Sets up:
      - CCSWITCH_HOME under tmp_path
      - Captures DEBUG logs from ccswitch.auth_login
      - Calls capture_login with the fake claude that writes
        accessToken:"fake-at", refreshToken:"fake-rt"

    Asserts:
      - Neither "fake-at" nor "fake-rt" appears in any log record's message or args
      - The slot was created (credentials stored successfully)
    """
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    # Capture DEBUG logs from the auth_login module + parent ccswitch
    caplog.set_level(logging.DEBUG, logger="ccswitch")
    caplog.set_level(logging.DEBUG, logger="ccswitch.auth_login")

    ok = auth_login.capture_login("primary", claude_bin=[sys.executable, FAKE_CLAUDE])
    assert ok is True, "capture_login must succeed with the fake claude"

    # Verify the slot was created
    assert paths.slot_path("primary").exists(), "credentials must be stored in the slot"

    # Scan all log records for token leakage
    for record in caplog.records:
        msg = str(record.getMessage())
        # Check the message itself
        assert FAKE_AT not in msg, (
            f"accessToken 'fake-at' leaked in log message: {msg}"
        )
        assert FAKE_RT not in msg, (
            f"refreshToken 'fake-rt' leaked in log message: {msg}"
        )
        # Check the record's args (in case formatting exposes them)
        if record.args:
            args_str = str(record.args)
            assert FAKE_AT not in args_str, (
                f"accessToken 'fake-at' leaked in log args: {args_str}"
            )
            assert FAKE_RT not in args_str, (
                f"refreshToken 'fake-rt' leaked in log args: {args_str}"
            )


@pytest.mark.asyncio
async def test_capture_login_no_token_leakage_in_audit(tmp_path, monkeypatch):
    """Run capture_login through the ReauthManager and verify audit detail.

    Sets up:
      - CCSWITCH_HOME under tmp_path
      - An in-memory SQLite DB (via init_db)
      - ReauthManager with a runner that calls capture_login

    Asserts:
      - The reauth succeeds (state='success')
      - audit_events rows for account.reauth.start/success have NO token material
    """
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent

    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    # Initialize in-memory DB
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    # Runner that calls capture_login (which will use the fake claude)
    async def runner_with_capture(name: str) -> bool:
        return auth_login.capture_login(
            name, claude_bin=[sys.executable, FAKE_CLAUDE]
        )

    m = ReauthManager(
        runner=runner_with_capture, key_available=lambda: True, timeout_s=5
    )
    await m.start("primary", "tester@example.com")

    # Wait for completion
    for _ in range(100):
        if m.status("primary").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)

    assert m.status("primary").state == "success"

    # The success audit row is written by the background _run task AFTER it flips
    # state to "success" (ReauthManager._run), so a read fired the instant state
    # turns "success" can race ahead of the audit commit. Poll for the rows to
    # land — mirroring the state poll above. (The old shared-connection StaticPool
    # test DB masked this race; the file-backed NullPool DB from #890 gives each
    # connection its own view, so the read must wait for the commit rather than
    # observing an in-flight write on a shared connection.)
    rows: list = []
    for _ in range(100):
        async with db_session() as db:
            result = await db.execute(
                select(AuditEvent).where(
                    AuditEvent.type.in_(["account.reauth.start", "account.reauth.success"])
                )
            )
            rows = result.scalars().all()
        if rows:
            break
        await asyncio.sleep(0.02)

    assert len(rows) > 0, "expected at least one audit row"

    for row in rows:
        detail_str = str(row.detail)
        assert FAKE_AT not in detail_str, (
            f"accessToken leaked in audit detail for {row.type}: {detail_str}"
        )
        assert FAKE_RT not in detail_str, (
            f"refreshToken leaked in audit detail for {row.type}: {detail_str}"
        )
        assert "subscriptionType" not in detail_str, (
            f"subscriptionType should not appear in audit detail for {row.type}: {detail_str}"
        )


def test_sweep_stale_removes_aged_dir_leaves_fresh(tmp_path, monkeypatch):
    """ReauthManager.sweep_stale() removes dirs older than 600s, keeps fresh ones.

    Sets up:
      - CCSWITCH_HOME under tmp_path
      - An old dir (mtime = now - 700s)
      - A fresh dir (mtime = now)

    Asserts:
      - Old dir is removed
      - Fresh dir is kept
    """
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    from ccswitch.paths import reauth_tmp_dir

    rtd = reauth_tmp_dir()
    rtd.mkdir(parents=True, exist_ok=True)

    # Create an old directory and backdate its mtime
    old_dir = rtd / "reauth-old-aged"
    old_dir.mkdir()
    old_time = time.time() - 700
    os.utime(old_dir, (old_time, old_time))

    # Create a fresh directory
    fresh_dir = rtd / "reauth-fresh-new"
    fresh_dir.mkdir()

    # Run sweep_stale
    m = ReauthManager(runner=lambda n: True, key_available=lambda: True, timeout_s=5)
    m.sweep_stale()

    # Verify old is gone, fresh remains
    assert not old_dir.exists(), "old dir (age > 600s) should have been removed"
    assert fresh_dir.exists(), "fresh dir should have been kept"
