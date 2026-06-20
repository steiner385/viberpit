"""Tests for ReauthManager — inject fake runner + short timeout; NO real browser/claude."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure src is importable when running from the repo root
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.reauth import ReauthManager, ReauthBusy, ReauthNoKey


# ---------------------------------------------------------------------------
# Plan-specified tests (verbatim from the plan)
# ---------------------------------------------------------------------------

def test_success_flow():
    async def runner(name): await asyncio.sleep(0.01); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)
    async def run():
        st = await m.start("primary", "me"); assert st.state == "running"
        for _ in range(50):
            if m.status("primary").state in ("success","error","timeout"): break
            await asyncio.sleep(0.01)
        return m.status("primary").state
    assert asyncio.run(run()) == "success"


def test_busy_raises():
    async def runner(name): await asyncio.sleep(0.2); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)
    async def run():
        await m.start("primary", "me")
        try:
            await m.start("secondary", "me"); return "nobusy"
        except ReauthBusy: return "busy"
    assert asyncio.run(run()) == "busy"


def test_no_key_raises():
    async def runner(name): return True
    m = ReauthManager(runner=runner, key_available=lambda: False, timeout_s=5)
    import pytest
    with pytest.raises(ReauthNoKey):
        asyncio.run(m.start("primary", "me"))


def test_timeout_marks_timeout():
    async def runner(name): await asyncio.sleep(5); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=0.05)
    async def run():
        await m.start("primary", "me")
        for _ in range(100):
            if m.status("primary").state in ("timeout","error","success"): break
            await asyncio.sleep(0.01)
        return m.status("primary").state
    assert asyncio.run(run()) == "timeout"


# ---------------------------------------------------------------------------
# Additional tests for coverage
# ---------------------------------------------------------------------------

def test_status_returns_snapshot():
    """status() returns a copy that matches the live state."""
    async def runner(name): await asyncio.sleep(0.1); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)

    async def run():
        await m.start("primary", "alice")
        snap = m.status("primary")
        return snap.state, snap.identity

    state, identity = asyncio.run(run())
    assert state == "running"
    assert identity == "alice"


def test_to_dict_camel_case():
    """ReauthState.to_dict() returns camelCase keys."""
    from server.reauth import ReauthState
    st = ReauthState(state="running", identity="me", started_at=1234567.0, message=None)
    d = st.to_dict()
    assert "startedAt" in d
    assert "state" in d
    assert "identity" in d
    assert "message" in d
    assert d["startedAt"] == 1234567.0
    assert "started_at" not in d


def test_cancel_sets_idle_when_running():
    """cancel() transitions running → idle after the task is stopped."""
    async def runner(name): await asyncio.sleep(10); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=30)

    async def run():
        await m.start("primary", "me")
        assert m.status("primary").state == "running"
        await m.cancel("primary")
        return m.status("primary").state

    assert asyncio.run(run()) == "idle"


def test_cancel_when_idle_is_noop():
    """cancel() on an idle manager returns idle, no exception."""
    async def runner(name): return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)

    async def run():
        return await m.cancel("primary")

    st = asyncio.run(run())
    assert st.state == "idle"


def test_runner_returning_false_marks_error():
    """If runner returns False the state transitions to 'error'."""
    async def runner(name): await asyncio.sleep(0.01); return False
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)

    async def run():
        await m.start("primary", "me")
        for _ in range(50):
            if m.status("primary").state not in ("running",): break
            await asyncio.sleep(0.01)
        return m.status("primary").state

    assert asyncio.run(run()) == "error"


def test_runner_exception_marks_error():
    """If runner raises an exception the state transitions to 'error'."""
    async def runner(name):
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)

    async def run():
        await m.start("primary", "me")
        for _ in range(50):
            if m.status("primary").state not in ("running",): break
            await asyncio.sleep(0.01)
        st = m.status("primary")
        return st.state, st.message

    state, msg = asyncio.run(run())
    assert state == "error"
    assert "boom" in (msg or "")


def test_sweep_stale_removes_old_dirs(tmp_path, monkeypatch):
    """sweep_stale() removes dirs older than 600s and leaves fresh ones."""
    import os, time, shutil

    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    from ccswitch.paths import reauth_tmp_dir
    rtd = reauth_tmp_dir()
    rtd.mkdir(parents=True, exist_ok=True)

    # Create an old dir (backdate mtime)
    old_dir = rtd / "reauth-old"
    old_dir.mkdir()
    old_time = time.time() - 700
    os.utime(old_dir, (old_time, old_time))

    # Create a fresh dir
    fresh_dir = rtd / "reauth-fresh"
    fresh_dir.mkdir()

    m = ReauthManager(runner=lambda n: True, key_available=lambda: True, timeout_s=5)
    m.sweep_stale()

    assert not old_dir.exists(), "stale dir should have been removed"
    assert fresh_dir.exists(), "fresh dir should have been kept"


def test_busy_exception_carries_identity():
    """ReauthBusy should carry the holding identity and start time."""
    async def runner(name): await asyncio.sleep(10); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=30)

    async def run():
        await m.start("primary", "holder@example.com")
        try:
            await m.start("secondary", "other@example.com")
        except ReauthBusy as exc:
            return exc.holding_identity, exc.started_at
        return None, None

    identity, started_at = asyncio.run(run())
    assert identity == "holder@example.com"
    assert started_at is not None


def test_shutdown_cancels_running():
    """shutdown() cancels a running reauth and returns cleanly."""
    async def runner(name): await asyncio.sleep(10); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=30)

    async def run():
        await m.start("primary", "me")
        assert m.status("primary").state == "running"
        await m.shutdown()
        return m.status("primary").state

    assert asyncio.run(run()) == "idle"
