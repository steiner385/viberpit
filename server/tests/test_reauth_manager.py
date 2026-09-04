"""Tests for ReauthManager — inject fake runner + short timeout; NO real browser/claude."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure src is importable when running from the repo root
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.reauth import ReauthManager, ReauthBusy, ReauthNoKey, REAUTH_DISPLAY_KEY


# ---------------------------------------------------------------------------
# Task 1: x11 accessor, reserved key, vnc_session
# ---------------------------------------------------------------------------

class _FakeLease:
    def __init__(self, display): self.display = display


class _FakeSpec:
    def __init__(self, display, xauth):
        self.lease = _FakeLease(display)
        self.xauthority = xauth


class _FakeX11:
    """Records start/stop and hands back a fixed display spec."""
    def __init__(self, spec=_FakeSpec(105, "/tmp/ccswitch-__reauth__.Xauthority")):
        self.spec = spec
        self.started = []
        self.stopped = []
    async def start_display(self, key):
        self.started.append(key)
        return self.spec
    async def stop_display(self, key):
        self.stopped.append(key)


def test_reauthstate_auth_url_serialises_roundtrip():
    from server.reauth import ReauthState
    s = ReauthState(state="awaiting_code", auth_url="https://claude.com/cai/oauth/authorize?x=1")
    assert s.to_dict()["authUrl"] == "https://claude.com/cai/oauth/authorize?x=1"
    assert s.to_dict()["state"] == "awaiting_code"
    back = ReauthState.from_dict(s.to_dict())
    assert back.auth_url == "https://claude.com/cai/oauth/authorize?x=1"
    assert back.state == "awaiting_code"


def test_manager_status_carries_auth_url():
    # The status() snapshot must propagate auth_url (this is the site that feeds the API).
    from server.reauth import ReauthManager
    m = ReauthManager(key_available=lambda: True)
    m._state.auth_url = "https://claude.com/cai/oauth/authorize?x=2"
    m._state.state = "awaiting_code"
    snap = m.status("acct")
    assert snap.auth_url == "https://claude.com/cai/oauth/authorize?x=2"
    assert snap.to_dict()["authUrl"] == "https://claude.com/cai/oauth/authorize?x=2"


def test_reauth_state_to_dict_includes_vnc_session():
    from server.reauth import ReauthState
    s = ReauthState(state="running", vnc_session="__reauth__")
    assert s.to_dict()["vncSession"] == "__reauth__"


def test_reauth_state_vnc_session_defaults_none():
    from server.reauth import ReauthState
    assert ReauthState().to_dict()["vncSession"] is None


def test_status_snapshot_surfaces_vnc_session():
    """The REAL ReauthManager.status() must copy vnc_session — the API/proxy read it via
    status(), so dropping it here ships the in-page VNC panel dark (regression guard: the
    test fakes re-add this field, so only a real-manager assertion catches the omission)."""
    mgr = ReauthManager(key_available=lambda: True)
    mgr._state.vnc_session = REAUTH_DISPLAY_KEY
    assert mgr.status("primary").to_dict()["vncSession"] == REAUTH_DISPLAY_KEY


@pytest.mark.asyncio
async def test_runner_allocates_virtual_display_and_releases(monkeypatch):
    # Capture the env_extra the runner passes to capture_login.
    captured = {}

    def fake_capture_login(name, env_extra=None):
        captured.update(env_extra or {})
        return True

    monkeypatch.setattr("ccswitch.auth_login.capture_login", fake_capture_login)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-x")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    fake = _FakeX11()
    mgr = ReauthManager(x11=lambda: fake, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    # wait for the background task to finish
    await asyncio.sleep(0.05)
    assert fake.started == [REAUTH_DISPLAY_KEY]
    assert fake.stopped == [REAUTH_DISPLAY_KEY]      # released in finally
    assert captured["DISPLAY"] == ":105"
    assert captured["XAUTHORITY"] == "/tmp/ccswitch-__reauth__.Xauthority"


@pytest.mark.asyncio
async def test_runner_falls_back_to_host_display_without_x11(monkeypatch):
    captured = {}
    monkeypatch.setattr("ccswitch.auth_login.capture_login",
                        lambda name, env_extra=None: captured.update(env_extra or {}) or True)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-y")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)   # no x11
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert captured["DISPLAY"] == ":0"          # settings.reauth_display default
    assert "XAUTHORITY" not in captured         # untouched fallback env


@pytest.mark.asyncio
async def test_prepare_reauth_profile_runs_off_the_event_loop(monkeypatch):
    """Finding 2: the profile copy (now tens of MB with the curated extension set) must
    run via asyncio.to_thread, not synchronously on the event loop — a synchronous call
    there stalls every other WebSocket session and API request for its duration."""
    import threading

    loop_thread_ident = threading.get_ident()
    seen: dict = {}

    def fake_prepare(profile, *, extension_ids=None):
        seen["thread_ident"] = threading.get_ident()
        try:
            asyncio.get_running_loop()
            seen["ran_on_loop"] = True
        except RuntimeError:
            seen["ran_on_loop"] = False
        return "/tmp/reauth-tmp-offloop"

    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", fake_prepare)
    monkeypatch.setattr("ccswitch.auth_login.capture_login", lambda name, env_extra=None: True)
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)

    assert seen.get("thread_ident") is not None, "fake prepare_reauth_profile must have been called"
    assert seen["thread_ident"] != loop_thread_ident, (
        "prepare_reauth_profile must not run on the event-loop thread"
    )
    assert seen["ran_on_loop"] is False, (
        "prepare_reauth_profile must not see a running event loop in its own thread"
    )


@pytest.mark.asyncio
async def test_runner_forwards_configured_extension_ids(monkeypatch):
    """CCSWITCH_REAUTH_EXTENSIONS reaches prepare_reauth_profile(extension_ids=...);
    unset → None so the core default (1Password) applies."""
    seen = []

    def fake_prepare(profile, *, extension_ids=None):
        seen.append(extension_ids)
        return "/tmp/reauth-tmp-z"

    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", fake_prepare)
    monkeypatch.setattr("ccswitch.auth_login.capture_login", lambda name, env_extra=None: True)
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "aaaa,bbbb")
    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert seen == [["aaaa", "bbbb"]]

    monkeypatch.delenv("CCSWITCH_REAUTH_EXTENSIONS")
    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert seen[-1] is None


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
    import os, time

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


def test_cancel_kills_via_launcher_pid_file(tmp_path, monkeypatch):
    """cancel() reads <temp>/launcher.pid and sends SIGTERM to that PID.

    Uses a fake runner that:
      1. Writes a sentinel PID into a launcher.pid file inside a temp dir.
      2. Sets manager._state._temp_profile to that temp dir (simulating what
         the production runner does immediately after prepare_reauth_profile).
      3. Then blocks until cancelled.

    Monkeypatches os.kill so no real signal is sent; asserts SIGTERM was
    attempted for the sentinel PID.
    """
    import os
    import signal

    SENTINEL_PID = os.getpid()  # use our own PID — guaranteed to exist

    # Write the launcher.pid file
    launcher_pid_file = tmp_path / "launcher.pid"
    launcher_pid_file.write_text(str(SENTINEL_PID))

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)

    # A runner that sets _temp_profile then blocks until cancelled
    async def fake_runner(name: str) -> bool:
        m._state._temp_profile = str(tmp_path)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        return False

    m = ReauthManager(runner=fake_runner, key_available=lambda: True, timeout_s=60)

    async def run():
        await m.start("primary", "fake@example.com")
        # Give the runner a tick to set _temp_profile before we cancel
        await asyncio.sleep(0.05)
        assert m._state._temp_profile == str(tmp_path), (
            "_temp_profile must be set by the runner before cancel"
        )
        await m.cancel("primary")

    asyncio.run(run())

    # _kill_launcher should have read tmp_path/launcher.pid and called os.kill
    sigterm_calls = [c for c in kill_calls if c == (SENTINEL_PID, signal.SIGTERM)]
    assert sigterm_calls, (
        f"Expected os.kill({SENTINEL_PID}, SIGTERM) but got: {kill_calls}"
    )


def test_sweep_stale_removes_symlinks(tmp_path, monkeypatch):
    """sweep_stale() removes symlinks without following them."""

    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    from ccswitch.paths import reauth_tmp_dir
    rtd = reauth_tmp_dir()
    rtd.mkdir(parents=True, exist_ok=True)

    # A real dir to be the symlink target (outside the sweep tree)
    real_target = tmp_path / "external-target"
    real_target.mkdir()

    # A symlink inside the sweep dir pointing to the real target
    sym = rtd / "reauth-symlink"
    sym.symlink_to(real_target)

    m = ReauthManager(runner=lambda n: True, key_available=lambda: True, timeout_s=5)
    m.sweep_stale()

    assert not sym.exists() and not sym.is_symlink(), "symlink should have been removed"
    assert real_target.exists(), "real target should not have been touched"


# ---------------------------------------------------------------------------
# Real DB-backed audit test (C2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_writes_audit_row_no_token_material():
    """ReauthManager emits account.reauth.success to audit_events on success.

    Uses a real in-memory SQLite DB (via init_db) so we can query the actual
    audit_events table.  Asserts:
      - exactly one account.reauth.success row is written
      - detail contains {"account": ...}
      - detail contains neither accessToken/refreshToken nor subscriptionType
    """
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent

    # Initialise a fresh in-memory DB for this test
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async def fake_runner(name: str) -> bool:
        await asyncio.sleep(0.01)
        return True

    m = ReauthManager(runner=fake_runner, key_available=lambda: True, timeout_s=5)
    await m.start("primary", "tester@example.com")

    # Wait for the background _run task to complete (success)
    for _ in range(100):
        if m.status("primary").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)

    assert m.status("primary").state == "success", "manager must reach success state"

    # The success audit row is committed by the background _run task AFTER it
    # flips state to "success" (ReauthManager._run), so a read fired the instant
    # state turns "success" can race ahead of that commit. Poll for the row to
    # land — mirroring the state poll above. (The old shared-connection StaticPool
    # test DB masked this race; the file-backed NullPool DB from #890 gives each
    # connection its own view, so the read must wait for the commit rather than
    # observing an in-flight write on a shared connection.)
    rows: list = []
    for _ in range(100):
        async with db_session() as db:
            result = await db.execute(
                select(AuditEvent).where(AuditEvent.type == "account.reauth.success")
            )
            rows = result.scalars().all()
        if rows:
            break
        await asyncio.sleep(0.02)

    assert len(rows) == 1, f"expected 1 account.reauth.success row, got {len(rows)}"
    detail = rows[0].detail
    assert "account" in detail, f"detail must contain 'account', got: {detail}"
    assert detail["account"] == "primary"

    # Verify no token/subscription material leaks into the audit row
    detail_str = str(detail)
    assert "accessToken" not in detail_str, "accessToken must not appear in audit detail"
    assert "refreshToken" not in detail_str, "refreshToken must not appear in audit detail"
    assert "subscriptionType" not in detail_str, "subscriptionType must not appear in audit detail"


# ---------------------------------------------------------------------------
# Phase B2b (new): in-process OAuth exchange via setup_token_oauth
# ---------------------------------------------------------------------------

import ccswitch.setup_token_oauth as _sto


def _make_manager(monkeypatch, tmp_path):
    """Construct a ReauthManager with key available and setup_token_oauth.begin() mocked."""
    from server.reauth import ReauthManager
    from ccswitch.setup_token_oauth import SetupTokenChallenge

    fake_challenge = SetupTokenChallenge(
        authorize_url="https://claude.com/cai/oauth/authorize?state=FAKE",
        verifier="fake-verifier",
        state="FAKE",
    )
    monkeypatch.setattr(_sto, "begin", lambda: fake_challenge)
    return ReauthManager(key_available=lambda: True)


@pytest.mark.asyncio
async def test_setup_token_start_enters_awaiting_code_without_subprocess(monkeypatch, tmp_path):
    """start(mode='setup_token') enters awaiting_code immediately — no background task."""
    mgr = _make_manager(monkeypatch, tmp_path)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    st = mgr.status("acct")
    assert st.state == "awaiting_code"
    assert "claude.com/cai/oauth/authorize" in (st.auth_url or "")
    # No background asyncio task should be running
    assert mgr._state._task is None


@pytest.mark.asyncio
async def test_setup_token_submit_code_stores_token(monkeypatch, tmp_path):
    """submit_code() on awaiting_code: exchanges code, stores token, marks reauthenticated."""
    mgr = _make_manager(monkeypatch, tmp_path)
    stored: dict = {}
    monkeypatch.setattr("ccswitch.crypto.store_oauth_token",
                        lambda name, tok: stored.__setitem__(name, tok))
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated",
                        lambda name: stored.__setitem__("reauth", name))
    monkeypatch.setattr(_sto, "exchange", lambda code, ch: "sk-ant-oat01-OK123")

    await mgr.start("acct", "id@example.com", mode="setup_token")
    # state embeds the FAKE state, so construct the code accordingly
    await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")

    assert stored.get("acct") == "sk-ant-oat01-OK123"
    assert stored.get("reauth") == "acct"
    assert mgr.status("acct").state == "success"


@pytest.mark.asyncio
async def test_setup_token_submit_invalid_code_sets_error(monkeypatch, tmp_path):
    """SetupTokenInvalidCode → state=error with user-facing message."""
    mgr = _make_manager(monkeypatch, tmp_path)

    def boom(code, ch):
        raise _sto.SetupTokenInvalidCode("bad")

    monkeypatch.setattr(_sto, "exchange", boom)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")

    st = mgr.status("acct")
    assert st.state == "error"
    assert "invalid or expired" in (st.message or "").lower()


@pytest.mark.asyncio
async def test_setup_token_submit_state_mismatch_sets_error(monkeypatch, tmp_path):
    """SetupTokenStateMismatch → state=error with restart message."""
    mgr = _make_manager(monkeypatch, tmp_path)

    def boom(code, ch):
        raise _sto.SetupTokenStateMismatch("mismatch")

    monkeypatch.setattr(_sto, "exchange", boom)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    await mgr.submit_code("acct", "CODE#WRONGSTATE")

    st = mgr.status("acct")
    assert st.state == "error"
    assert "restart" in (st.message or "").lower()


@pytest.mark.asyncio
async def test_setup_token_submit_transient_sets_error(monkeypatch, tmp_path):
    """SetupTokenTransient → state=error with try-again message."""
    mgr = _make_manager(monkeypatch, tmp_path)

    def boom(code, ch):
        raise _sto.SetupTokenTransient("network")

    monkeypatch.setattr(_sto, "exchange", boom)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")

    st = mgr.status("acct")
    assert st.state == "error"
    assert "try again" in (st.message or "").lower()


@pytest.mark.asyncio
async def test_submit_code_noop_when_not_awaiting():
    """submit_code() when state is idle returns a snapshot and does not crash."""
    from server.reauth import ReauthManager

    m = ReauthManager(key_available=lambda: True)
    snap = await m.submit_code("acct", "ANYCODE")
    assert snap.state == "idle"


@pytest.mark.asyncio
async def test_setup_token_cancel_during_awaiting_code_returns_idle(monkeypatch, tmp_path):
    """cancel() during awaiting_code transitions to idle promptly (no background thread to unblock)."""
    import time
    mgr = _make_manager(monkeypatch, tmp_path)
    await mgr.start("acct", "id@example.com", mode="setup_token")
    assert mgr.status("acct").state == "awaiting_code"

    t0 = time.monotonic()
    await mgr.cancel("acct")
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"cancel should be prompt; took {elapsed:.2f}s"
    assert mgr.status("acct").state == "idle"


@pytest.mark.asyncio
async def test_start_mode_login_uses_login_runner(monkeypatch):
    """mode='login' (default) still uses the login runner, not setup_token."""
    from ccswitch import auth_login
    from server.reauth import ReauthManager

    setup_token_begin_called = []
    monkeypatch.setattr(_sto, "begin", lambda: setup_token_begin_called.append(True) or (_ for _ in ()).throw(AssertionError("begin() called for login mode")))
    monkeypatch.setattr(auth_login, "capture_login",
                        lambda name, env_extra=None: True)
    monkeypatch.setattr(auth_login, "prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-mode")
    monkeypatch.setattr(auth_login, "cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    m = ReauthManager(key_available=lambda: True, timeout_s=5)
    await m.start("acct3", "me@x", mode="login")

    for _ in range(50):
        if m.status("acct3").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)

    assert not setup_token_begin_called, "setup_token_oauth.begin() should NOT be called for mode='login'"
    assert m.status("acct3").state == "success"


# ---------------------------------------------------------------------------
# Fix M-A + M-C: double-submit guard and unexpected-exception handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_submit_is_noop(monkeypatch, tmp_path):
    """M-A: second sequential submit_code call is a no-op — exchange called exactly once."""
    mgr = _make_manager(monkeypatch, tmp_path)

    call_count = 0

    def counting_exchange(code, ch):
        nonlocal call_count
        call_count += 1
        return "sk-ant-oat01-GOODTOKEN"

    monkeypatch.setattr(_sto, "exchange", counting_exchange)
    monkeypatch.setattr("ccswitch.crypto.store_oauth_token", lambda name, tok: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda name: None)

    await mgr.start("acct", "id@example.com", mode="setup_token")
    code = f"CODE#{mgr._setup_challenge.state}"

    # First call — should exchange and succeed.
    st1 = await mgr.submit_code("acct", code)
    # Second call — state is no longer awaiting_code; must be a no-op.
    st2 = await mgr.submit_code("acct", code)

    assert call_count == 1, f"exchange must be called exactly once; got {call_count}"
    assert st1.state == "success"
    assert st2.state == "success"  # snapshot of current state — still success


@pytest.mark.asyncio
async def test_unexpected_store_failure_sets_error_not_stuck(monkeypatch, tmp_path):
    """M-C: RuntimeError during crypto.store_oauth_token → state=error, message excludes token."""
    mgr = _make_manager(monkeypatch, tmp_path)

    GOOD_TOKEN = "sk-ant-oat01-SECRETTOKEN"
    monkeypatch.setattr(_sto, "exchange", lambda code, ch: GOOD_TOKEN)

    def boom(name, tok):
        raise RuntimeError("boom")

    monkeypatch.setattr("ccswitch.crypto.store_oauth_token", boom)

    await mgr.start("acct", "id@example.com", mode="setup_token")
    st = await mgr.submit_code("acct", f"CODE#{mgr._setup_challenge.state}")

    assert st.state == "error", f"expected error, got {st.state!r}"
    assert st.state != "awaiting_code", "state must not be stuck in awaiting_code"
    assert GOOD_TOKEN not in (st.message or ""), "token must not appear in the error message"


# ---------------------------------------------------------------------------
# Bug 1: a terminal 'success' from one account must not leak into another
# account's *scoped* status poll. The single global _state made success
# "sticky": the next account's first poll saw the prior account's success,
# the frontend stopped polling on the terminal state, and reauth on the new
# account froze. status(name, scoped=True) returns idle for any account that
# does not own the current state. Unscoped status() stays raw (add-flow compat).
# ---------------------------------------------------------------------------

def test_scoped_status_does_not_leak_success_to_other_account():
    async def runner(name): await asyncio.sleep(0.01); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)

    async def run():
        await m.start("acctA", "a@example.com")
        for _ in range(50):
            if m.status("acctA").state in ("success", "error", "timeout"): break
            await asyncio.sleep(0.01)
        return (
            m.status("acctA", scoped=True).state,
            m.status("acctB", scoped=True).state,
            m.status("acctB").state,  # unscoped: raw global (add-flow compat)
            m.status("acctA", scoped=True).to_dict().get("account"),
        )

    a_scoped, b_scoped, b_unscoped, a_account = asyncio.run(run())
    assert a_scoped == "success", "owning account still sees its terminal state"
    assert b_scoped == "idle", "a different account must not see the leaked success"
    assert b_unscoped == "success", "unscoped status stays raw for the add flow"
    assert a_account == "acctA", "wire format exposes the owning account"


def test_scoped_status_passes_through_when_no_active_account():
    """A fresh manager (no reauth started) reports idle to any scoped poll."""
    m = ReauthManager(runner=lambda n: True, key_available=lambda: True, timeout_s=5)
    assert m.status("whoever", scoped=True).state == "idle"


# ---------------------------------------------------------------------------
# Progress feedback: the pre-browser wait must not be silent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_publishes_progress_for_the_virtual_display(monkeypatch):
    """The step label is live while capture_login blocks, and names the virtual display.

    Profile copy + Xvfb spawn + Chrome cold start take several seconds, during which
    the viewer shows only "Reconnecting…". The UI polls status every 1.5s, so the
    message must be set *before* the blocking call, not after it."""
    seen = {}

    def fake_capture_login(name, env_extra=None):
        seen["message"] = mgr._state.message      # sampled mid-run, not after
        seen["state"] = mgr._state.state
        return True

    monkeypatch.setattr("ccswitch.auth_login.capture_login", fake_capture_login)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-p")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=lambda: _FakeX11(), key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)

    assert seen["state"] == "running"
    assert seen["message"], "the wait before the browser appears must not be silent"
    assert "host screen" not in seen["message"], (
        f"virtual-display mode must not claim the host screen: {seen['message']!r}"
    )
    assert "few seconds" in seen["message"], f"unexpected label: {seen['message']!r}"


@pytest.mark.asyncio
async def test_runner_progress_names_the_host_screen_without_x11(monkeypatch):
    """Without a virtual display the label must point at the host screen instead."""
    seen = {}
    monkeypatch.setattr("ccswitch.auth_login.capture_login",
                        lambda name, env_extra=None: seen.update(message=mgr._state.message) or True)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-q")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)

    assert "host screen" in (seen.get("message") or ""), (
        f"host-screen fallback must say so: {seen.get('message')!r}"
    )


@pytest.mark.asyncio
async def test_progress_does_not_clobber_a_terminal_message(monkeypatch):
    """A progress label written late must never overwrite a terminal failure message."""
    monkeypatch.setattr("ccswitch.auth_login.capture_login",
                        lambda name, env_extra=None: False)          # login fails
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p, **kw: "/tmp/reauth-tmp-r")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)

    st = mgr.status("primary")
    assert st.state in ("error", "timeout"), f"expected a terminal state, got {st.state!r}"
    assert "host screen" not in (st.message or ""), (
        f"a progress label leaked into the terminal message: {st.message!r}"
    )


# ---------------------------------------------------------------------------
# Fix #735: cancelled/timed-out reauth must clean up the temp Chrome profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_cleans_up_temp_profile(tmp_path, monkeypatch):
    """cancel() must remove the temp profile directory even when the runner thread is still live.

    The runner's finally block cannot fire while asyncio.to_thread is blocked,
    so the state machine's terminal transition must drive the cleanup.
    """
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    from ccswitch.paths import reauth_tmp_dir
    rtd = reauth_tmp_dir()
    rtd.mkdir(parents=True, exist_ok=True)

    temp_profile = rtd / "reauth-abc123"
    temp_profile.mkdir(mode=0o700)

    runner_ready = asyncio.Event()

    async def fake_runner(name: str) -> bool:
        m._state._temp_profile = str(temp_profile)
        runner_ready.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        return False

    m = ReauthManager(runner=fake_runner, key_available=lambda: True, timeout_s=60)
    await m.start("primary", "fake@example.com")
    await runner_ready.wait()

    assert temp_profile.exists(), "profile dir must exist before cancel"
    await m.cancel("primary")

    assert not temp_profile.exists(), "cancel() must remove the temp profile directory"
    assert m._state._temp_profile is None, "_temp_profile pointer must be cleared"
    assert m.status("primary").state == "idle"


@pytest.mark.asyncio
async def test_timeout_cleans_up_temp_profile(tmp_path, monkeypatch):
    """_run() timeout must remove the temp profile directory."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))

    from ccswitch.paths import reauth_tmp_dir
    rtd = reauth_tmp_dir()
    rtd.mkdir(parents=True, exist_ok=True)

    temp_profile = rtd / "reauth-def456"
    temp_profile.mkdir(mode=0o700)

    runner_started = asyncio.Event()

    async def fake_runner(name: str) -> bool:
        m._state._temp_profile = str(temp_profile)
        runner_started.set()
        await asyncio.sleep(60)  # will be cut short by the 50ms timeout
        return False

    m = ReauthManager(runner=fake_runner, key_available=lambda: True, timeout_s=0.05)
    await m.start("primary", "fake@example.com")
    await runner_started.wait()

    for _ in range(100):
        if m.status("primary").state in ("timeout", "error"):
            break
        await asyncio.sleep(0.01)

    assert m.status("primary").state == "timeout"
    assert not temp_profile.exists(), "timeout must remove the temp profile directory"
    assert m._state._temp_profile is None
