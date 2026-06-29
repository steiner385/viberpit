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
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p: "/tmp/reauth-tmp-x")
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
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p: "/tmp/reauth-tmp-y")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)   # no x11
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert captured["DISPLAY"] == ":0"          # settings.reauth_display default
    assert "XAUTHORITY" not in captured         # untouched fallback env


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

    # Query the audit_events table directly
    async with db_session() as db:
        result = await db.execute(
            select(AuditEvent).where(AuditEvent.type == "account.reauth.success")
        )
        rows = result.scalars().all()

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
# Phase B2b: setup-token runner + async↔thread bridge + submit_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_token_flow_awaiting_code_then_submit_succeeds(monkeypatch):
    """Full bridge: fake capture drives on_url → awaiting_code, then submit_code
    unblocks get_code() → runner returns True → state == success."""
    from ccswitch import auth_login
    from server.reauth import ReauthManager

    got: dict = {}

    def fake_capture(name, *, on_url, get_code, claude_bin=None, env_extra=None):
        on_url("https://claude.com/cai/oauth/authorize?x=1")
        code = get_code()          # blocks until submit_code
        got["code"] = code
        return True

    monkeypatch.setattr(auth_login, "capture_setup_token", fake_capture)

    m = ReauthManager(key_available=lambda: True)
    await m.start("acct", "me@x", mode="setup_token")

    # wait for the runner thread to surface the URL → awaiting_code
    for _ in range(100):
        if m.status("acct").state == "awaiting_code":
            break
        await asyncio.sleep(0.02)
    snap = m.status("acct")
    assert snap.state == "awaiting_code", f"expected awaiting_code, got {snap.state!r}"
    assert snap.auth_url == "https://claude.com/cai/oauth/authorize?x=1"

    await m.submit_code("acct", "PASTEDCODE")

    # wait for completion
    for _ in range(100):
        if m.status("acct").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)
    assert m.status("acct").state == "success", (
        f"expected success after submit, got {m.status('acct').state!r}"
    )
    assert got["code"] == "PASTEDCODE"


@pytest.mark.asyncio
async def test_submit_code_noop_when_not_awaiting():
    """submit_code() when state is idle returns a snapshot and does not crash."""
    from server.reauth import ReauthManager

    m = ReauthManager(key_available=lambda: True)
    # state is idle — submit_code should be a no-op
    snap = await m.submit_code("acct", "ANYCODE")
    assert snap.state == "idle"


@pytest.mark.asyncio
async def test_setup_token_runner_false_marks_error(monkeypatch):
    """If fake_capture returns False the state transitions to 'error'."""
    from ccswitch import auth_login
    from server.reauth import ReauthManager

    def fake_capture(name, *, on_url, get_code, claude_bin=None, env_extra=None):
        on_url("https://claude.com/cai/oauth/authorize?x=99")
        get_code()
        return False

    monkeypatch.setattr(auth_login, "capture_setup_token", fake_capture)

    m = ReauthManager(key_available=lambda: True)
    await m.start("acct2", "me@x", mode="setup_token")

    for _ in range(100):
        if m.status("acct2").state == "awaiting_code":
            break
        await asyncio.sleep(0.02)

    await m.submit_code("acct2", "SOMECODE")

    for _ in range(100):
        if m.status("acct2").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)
    assert m.status("acct2").state == "error"


@pytest.mark.asyncio
async def test_start_mode_login_uses_login_runner(monkeypatch):
    """mode='login' (default) still uses the login runner, not setup_token."""
    from ccswitch import auth_login
    from server.reauth import ReauthManager

    setup_token_called = []

    def fake_capture_setup(name, *, on_url, get_code, claude_bin=None, env_extra=None):
        setup_token_called.append(True)
        return True

    monkeypatch.setattr(auth_login, "capture_setup_token", fake_capture_setup)
    monkeypatch.setattr(auth_login, "capture_login",
                        lambda name, env_extra=None: True)
    monkeypatch.setattr(auth_login, "prepare_reauth_profile", lambda p: "/tmp/reauth-tmp-mode")
    monkeypatch.setattr(auth_login, "cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    m = ReauthManager(key_available=lambda: True, timeout_s=5)
    await m.start("acct3", "me@x", mode="login")

    for _ in range(50):
        if m.status("acct3").state in ("success", "error", "timeout"):
            break
        await asyncio.sleep(0.02)

    assert not setup_token_called, "setup_token runner should NOT be used for mode='login'"
