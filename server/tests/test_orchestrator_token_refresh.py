"""Thread 2 — proactive idle-token refresh, decoupled from the usage poller's
backoff. Keeps idle accounts' access tokens fresh (and recovers quickly after a
daemon restart) even when the usage endpoint is throttled.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


_ACCOUNTS_TARGET = "server.sessions.orchestrator.ccswitch_accounts"


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


@pytest.mark.asyncio
async def test_refresh_idle_tokens_enumerates_accounts_off_event_loop():
    """A contended account ledger must not block the daemon's event loop."""
    orch = _orch()
    loop_thread = threading.get_ident()
    enumeration_threads = []

    def list_accounts():
        enumeration_threads.append(threading.get_ident())
        return [{"name": "idle", "status": "ok"}]

    orch.has_live_session = lambda name: False
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.list_accounts.side_effect = list_accounts
        await orch._refresh_idle_tokens_once()
    assert len(enumeration_threads) == 1
    assert enumeration_threads[0] != loop_thread
    ca.acquire_valid_token.assert_called_once_with("idle")


@pytest.mark.asyncio
async def test_worker_never_sweeps_idle_tokens(monkeypatch):
    """#2625: the primary is the fleet's only refresher. A worker's sweep does not
    even enumerate accounts — the real role gate (env → fleet_role → accounts) is
    exercised, not a patched predicate."""
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    orch = _orch()
    orch.has_live_session = lambda name: False
    from viberpit import accounts as real_accounts
    with mock.patch.object(real_accounts, "list_accounts",
                           return_value=[{"name": "idle", "status": "ok"}]) as la, \
            mock.patch.object(real_accounts, "acquire_valid_token") as avt:
        await orch._refresh_idle_tokens_once()
    la.assert_not_called()
    avt.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_idle_tokens_handles_account_enumeration_failure():
    orch = _orch()
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.list_accounts.side_effect = OSError("ledger unavailable")
        await orch._refresh_idle_tokens_once()
    ca.acquire_valid_token.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_idle_tokens_is_best_effort():
    """A refresh raising (AuthError/TransientError/anything) must not abort the
    pass — other accounts still get their turn."""
    orch = _orch()
    orch.has_live_session = lambda name: False
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.list_accounts.return_value = [{"name": "a", "status": "ok"}, {"name": "b", "status": "ok"}]
        ca.acquire_valid_token.side_effect = [RuntimeError("boom"), None]
        await orch._refresh_idle_tokens_once()  # must not raise
    assert [c.args[0] for c in ca.acquire_valid_token.call_args_list] == ["a", "b"]


@pytest.mark.asyncio
async def test_mark_needs_reauth_writes_audit_event():
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    orch = _orch()
    await orch._mark_needs_reauth_audit("primary")
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1 and rows[0].detail["path"] == "live-session" and rows[0].detail["account"] == "primary"


@pytest.mark.asyncio
async def test_mark_needs_reauth_audit_records_token_expiry(monkeypatch):
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    from server.sessions import orchestrator as orch_mod
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    orch = _orch()
    ca = mock.MagicMock()
    ca._slot_expires_at.return_value = 1893456000000  # fixed ms-epoch
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)
    await orch._mark_needs_reauth_audit("primary")
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["token_expires_at"] == 1893456000000
    ca._slot_expires_at.assert_called_once_with("primary")


@pytest.mark.asyncio
async def test_start_launches_token_refresh_monitor_and_stop_cancels():
    orch = _orch()
    orch._refresh_idle_tokens_once = mock.AsyncMock()
    # Avoid the real poller/PR-poller and sibling monitors doing work in the test.
    orch._poller = mock.AsyncMock()
    orch._pr_poller = mock.AsyncMock()
    orch._rotation_monitor = mock.AsyncMock()
    orch._titling_monitor = mock.AsyncMock()
    await orch.start()
    assert orch._token_refresh_task is not None
    # The monitor runs an immediate pass on startup (restart recovery).
    for _ in range(10):
        await asyncio.sleep(0)
        if orch._refresh_idle_tokens_once.await_count:
            break
    assert orch._refresh_idle_tokens_once.await_count >= 1
    task = orch._token_refresh_task
    await orch.stop()
    assert task.cancelled() or task.done()
    assert orch._token_refresh_task is None  # stop() nulls the handle


def test_poller_usage_fn_tags_path_poller_idle(monkeypatch):
    """The default _poller_usage_fn must pass path="poller-idle" to usage_for."""
    import server.sessions.orchestrator as orch_mod
    seen: dict = {}

    def fake_usage_for(name, *, allow_refresh=True, path="unknown"):
        seen["path"] = path
        seen["name"] = name
        return None

    monkeypatch.setattr(orch_mod.ccswitch_accounts, "usage_for", fake_usage_for)
    # Build WITHOUT a usage_fn so the real _poller_usage_fn wrapper is created.
    orch = orch_mod.SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0)
    orch._poller._usage_fn("primary")  # call the wired poller fn directly
    assert seen == {"path": "poller-idle", "name": "primary"}


def test_session_factory_injects_oauth_token_env(monkeypatch):
    """Session factory must inject CLAUDE_CODE_OAUTH_TOKEN for accounts with a setup-token."""
    captured = {}
    def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return object()
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token = lambda acct: "sk-ant-oat01-TOK" if acct == "primary" else None
    factory = orch._session_factory("s1")
    factory("/tmp/x", "/cfg", None, "default", None)
    assert captured["extra_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"


def test_session_factory_no_token_leaves_env_unchanged(monkeypatch):
    """When no setup-token exists for the account, factory must NOT inject
    CLAUDE_CODE_OAUTH_TOKEN and (with no X11) must call the base factory WITHOUT extra_env."""
    captured = {}
    def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return object()
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token = lambda acct: None
    factory = orch._session_factory("s1")
    factory("/tmp/x", "/cfg", None, "default", None)
    # no token + no x11 → base called with extra_env=None (unchanged old path)
    assert captured["extra_env"] is None


# ---------------------------------------------------------------------------
# Remote Control (plan 2026-11-24 Tasks A1/B): a live session only blocks the sweep when
# it may own the slot's token family. That is decided from what each live session
# ACTUALLY launched with (``SessionRecord.launch_credential``, stamped at spawn), never
# from the current store: a cfg-dir launch (the CLI rotates the family itself) or an
# unrecorded one owns it; a slot / setup / envelope env-token launch does not.
# ---------------------------------------------------------------------------


def _live(orch, account: str, launch: str | None, sid: str | None = None) -> SessionRecord:
    """Plant a live local session on *account* that launched with *launch*."""
    sid = sid or f"{account}-{launch}-{len(orch._records)}"
    rec = SessionRecord(id=sid, name=sid, account=account, engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    rec.launch_credential = launch
    orch._records[sid] = rec
    orch._drivers[sid] = SimpleNamespace(account=account)
    return rec


@pytest.mark.asyncio
async def test_refresh_idle_tokens_skips_needs_reauth_and_owned_families():
    orch = _orch()
    accts = [
        {"name": "idle_ok", "status": "ok"},
        {"name": "disabled", "status": "ok", "enabled": False},
        {"name": "dead", "status": "needs-reauth"},   # dead token — don't hammer
        {"name": "live_ok", "status": "ok"},           # the CLI owns the token family
    ]
    _live(orch, "live_ok", "cfg")
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.list_accounts.return_value = accts
        await orch._refresh_idle_tokens_once()
    refreshed = [c.args[0] for c in ca.acquire_valid_token.call_args_list]
    assert refreshed == ["idle_ok"], f"only the idle healthy account should refresh: {refreshed}"


def test_env_token_launches_do_not_own_the_slot_family():
    orch = _orch()
    for kind in ("slot", "setup", "envelope"):
        _live(orch, "acct", kind)
    assert orch.has_live_session("acct") is True
    assert orch.live_session_owns_slot_family("acct") is False
    assert orch.live_session_owns_slot_family("idle") is False


def test_a_cfg_launch_owns_the_family_even_after_a_setup_token_is_added():
    """The TOCTOU the store-derived check had: a session launched from the cfg-dir
    claudeAiOauth BEFORE a setup-token was added still rotates that family. Adding the
    token must not make the authority refresh it out from under the running CLI."""
    orch = _orch()
    _live(orch, "acct", "cfg")
    _live(orch, "acct", "setup")
    with mock.patch("server.sessions.account_auth.crypto") as crypto:
        crypto.read_oauth_token.return_value = "sk-ant-oat01-NEW"   # added after launch
        assert orch.live_session_owns_slot_family("acct") is True


def test_an_unrecorded_launch_owns_the_family():
    """Fail closed: a rehydrated or adopted session whose launch was never stamped."""
    orch = _orch()
    _live(orch, "acct", None)
    assert orch.live_session_owns_slot_family("acct") is True


def test_a_copilot_launch_keeps_counting_as_owner():
    orch = _orch()
    _live(orch, "acct", "copilot")
    assert orch.live_session_owns_slot_family("acct") is True


def test_a_remote_fleet_session_is_judged_by_its_recorded_launch():
    orch = _orch()
    remote = SessionRecord(id="r1", name="r1", account="acct", engine=AgentEngine.CLAUDE_CODE,
                           engine_session_id="", cols=80, rows=24, cwd="/tmp", status="idle",
                           host="some-other-host")
    orch._records["r1"] = remote
    assert orch.live_session_owns_slot_family("acct") is True      # unknown → owned
    remote.launch_credential = "envelope"
    assert orch.live_session_owns_slot_family("acct") is False


def test_ownership_never_reads_the_store():
    orch = _orch()
    _live(orch, "acct", "setup")
    with mock.patch(_ENV_TARGET, side_effect=AssertionError("store read")), \
            mock.patch("server.sessions.account_auth.crypto") as crypto:
        crypto.read_oauth_token.side_effect = AssertionError("store read")
        assert orch.live_session_owns_slot_family("acct") is False


_ENV_TARGET = "server.sessions.orchestrator.oauth_env_for"


@pytest.mark.asyncio
async def test_refresh_sweep_refreshes_a_live_account_whose_sessions_use_an_env_token():
    orch = _orch()
    accts = [{"name": "live_env", "status": "ok"}, {"name": "live_cfg", "status": "ok"}]
    _live(orch, "live_env", "setup")
    _live(orch, "live_cfg", "cfg")
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.list_accounts.return_value = accts
        await orch._refresh_idle_tokens_once()
    assert [c.args[0] for c in ca.acquire_valid_token.call_args_list] == ["live_env"]


def test_poller_refreshes_a_live_account_whose_sessions_use_an_env_token():
    """The usage poller asks the same ownership question as the sweep."""
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0)
    _live(orch, "env", "setup")
    _live(orch, "cfg", "cfg")
    calls = []
    with mock.patch(_ACCOUNTS_TARGET) as ca:
        ca.usage_for.side_effect = lambda n, **kw: calls.append((n, kw["allow_refresh"]))
        orch._poller._usage_fn("env")
        orch._poller._usage_fn("cfg")
    assert calls == [("env", True), ("cfg", False)]


# ---------------------------------------------------------------------------
# The launch credential is stamped by the factory at every spawn.
# ---------------------------------------------------------------------------


def _factory_orch(base):
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    return orch


@pytest.mark.parametrize("token,kind", [("sk-ant-oat01-TOK", "setup"), (None, "cfg")])
def test_the_factory_stamps_the_launch_credential(monkeypatch, token, kind):
    orch = _factory_orch(lambda *a, **k: object())
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token = lambda acct: token
    orch._session_factory("s1")("/tmp/x", "/cfg", None, "default", None)
    assert orch._records["s1"].launch_credential == kind


def test_an_envelope_launch_is_stamped_envelope(monkeypatch):
    orch = _factory_orch(lambda *a, **k: object())
    with mock.patch("server.sessions.account_auth._accounts.runner_uses_envelope",
                    return_value=True), \
            mock.patch("server.sessions.account_auth.credential_envelope") as ce:
        ce.runner_store.return_value.launch_token.return_value = "ENV-TOKEN"
        orch._session_factory("s1")("/tmp/x", "/cfg", None, "default", None)
    assert orch._records["s1"].launch_credential == "envelope"


def test_a_failed_store_read_leaves_the_launch_unrecorded(monkeypatch):
    orch = _factory_orch(lambda *a, **k: object())
    orch._records["s1"].launch_credential = "setup"
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token.side_effect = OSError("sshfs")
    with pytest.raises(OSError):
        orch._session_factory("s1")("/tmp/x", "/cfg", None, "default", None)
    assert orch._records["s1"].launch_credential is None


def test_launch_credential_rides_the_record_wire_format():
    rec = SessionRecord(id="s", name="s", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="", cols=80, rows=24, cwd="/tmp")
    rec.launch_credential = "envelope"
    assert SessionRecord.from_dict(rec.to_dict()).launch_credential == "envelope"
