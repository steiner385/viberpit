"""Native display routing inside the orchestrator (spec §4.6).

Pattern: server/tests/test_orchestrator_copilot_env.py — the fake SDK factory records
the extra_env the orchestrator hands it, which is where DISPLAY lands.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


class FakeNative:
    def __init__(self, available=True, display=":0"):
        self._available = available; self.display = display
        self.launched: list[tuple[str, str | None]] = []; self.closed: list[str] = []
    def available(self): return self._available
    def env(self): return {"DISPLAY": self.display}
    async def launch_browser(self, sid, url): self.launched.append((sid, url)); return True
    async def close(self, sid): self.closed.append(sid)
    async def stop_all(self): ...


class FakeX11:
    def __init__(self):
        self.started: list[str] = []; self.stopped: list[str] = []; self.launched = []
    async def start_display(self, sid): self.started.append(sid); return object()
    async def stop_display(self, sid): self.stopped.append(sid)
    def env_for(self, sid):
        return {"DISPLAY": ":100", "XAUTHORITY": f"/tmp/{sid}.Xauthority"} if sid in self.started else {}
    def display_for(self, sid): return object() if sid in self.started else None
    async def launch_browser(self, sid, url): self.launched.append((sid, url)); return True


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed(orch, sid, **kw):
    orch._records[sid] = SessionRecord(
        id=sid, name="t", account="acct", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="", cols=80, rows=24, cwd="/tmp", **kw)


# --- _display_env / _session_factory (unit, no DB) ---------------------------------

def test_factory_injects_native_display_for_native_record():
    seen = {}
    def rec_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class _F:
            async def connect(self, *a, **k): ...
            async def disconnect(self): ...
        return _F()
    orch = SessionOrchestrator(sdk_factory=rec_factory, x11=FakeX11())
    orch._native = FakeNative()
    _seed(orch, "s1", display_mode="native")
    orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert seen["extra_env"]["DISPLAY"] == ":0"
    assert "XAUTHORITY" not in seen["extra_env"]


def test_factory_uses_x11_env_for_virtual_record():
    seen = {}
    def rec_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class _F: ...
        return _F()
    x11 = FakeX11(); x11.started.append("s1")
    orch = SessionOrchestrator(sdk_factory=rec_factory, x11=x11)
    orch._native = FakeNative()
    _seed(orch, "s1", display_mode="virtual")
    orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert seen["extra_env"]["DISPLAY"] == ":100"


def test_native_record_without_desktop_degrades_to_no_display():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), x11=FakeX11())
    orch._native = FakeNative(available=False)
    _seed(orch, "s1", display_mode="native")
    assert orch._display_env("s1") == {}


# --- create_session end to end -----------------------------------------------------

@pytest.fixture
def spawn_env(tmp_path):
    shared = tmp_path / "dot-claude"; shared.mkdir()
    (shared / "settings.json").write_text("{}"); (shared / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "acct"; cfg_dir.mkdir(parents=True)
    (tmp_path / "repos").mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    from ccswitch.selector import Picked
    patches = [
        patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path / "home"),
                                "CLAUDE_CONFIG_DIR": str(shared), "CCSWITCH_DB_URL": db_url,
                                "CCSWITCH_HOST_NAME": "dobby"}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("acct")),
    ]
    for p in patches: p.start()
    yield db_url, str(tmp_path / "repos")
    for p in patches: p.stop()


async def _orch(spawn_env, x11, native, *, max_active_sessions=None):
    from server.db import init_db
    db_url, repo_root = spawn_env
    await init_db(db_url=db_url)
    if max_active_sessions is not None:
        from server.db import db_session
        from server.db.models import AppSettings
        async with db_session() as db:
            db.add(AppSettings(id="singleton", max_active_sessions=max_active_sessions))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), x11=x11,
                               poll_interval=9999.0, usage_fn=_fake_usage, repo_root=repo_root)
    orch._native = native
    await orch.start()
    return orch


@pytest.mark.asyncio
async def test_local_client_gets_native_and_no_xvfb_lease(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        assert rec.display_mode == "native"
        assert x11.started == []
        assert orch._display_env(rec.id) == {"DISPLAY": ":0"}
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            row = await db.get(DbSession, rec.id)
        assert row.display_mode == "native"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_remote_client_gets_virtual(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="phone")
        assert rec.display_mode == "virtual"
        assert x11.started == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_explicit_virtual_from_local_client(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby", display="virtual")
        assert rec.display_mode == "virtual" and x11.started == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_no_native_display_wired_means_virtual(spawn_env):
    x11 = FakeX11()
    orch = await _orch(spawn_env, x11, None)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        assert rec.display_mode == "virtual"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_close_tears_down_native_browsers(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        await orch.close_session(rec.id)
        assert native.closed == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_open_browser_dispatches_by_mode(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        nat = await orch.create_session("n", account="acct", client_node="dobby")
        vir = await orch.create_session("v", account="acct", client_node="phone")
        assert await orch.open_browser(nat.id, "https://a") == {"ok": True, "reason": None}
        assert native.launched == [(nat.id, "https://a")]
        assert await orch.open_browser(vir.id, None) == {"ok": True, "reason": None}
        assert x11.launched == [(vir.id, None)]
        assert await orch.open_browser("nope", None) == {"ok": False, "reason": "no_display"}
        native._available = False
        assert await orch.open_browser(nat.id, None) == {"ok": False, "reason": "native_unavailable"}
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_rehydrate_keeps_native_and_skips_lease(spawn_env):
    """A daemon restart must re-inject :0, not silently hand the session an Xvfb lease."""
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        sid = rec.id
    finally:
        await orch.stop()
    x11b, nativeb = FakeX11(), FakeNative()
    orch2 = await _orch(spawn_env, x11b, nativeb)
    try:
        await orch2.rehydrate()
        assert orch2._records[sid].display_mode == "native"
        drv = await orch2.ensure_driver(sid)
        assert drv is not None
        assert x11b.started == []
        assert orch2._display_env(sid) == {"DISPLAY": ":0"}
    finally:
        await orch2.stop()


# --- queue / approval holds must not lose the display decision --------------------
#
# Round-1 review finding: create_session forwarded client_node/display only on the
# DIRECT spawn. A session that instead queues (_enqueue_session) or parks for model
# approval (_hold_for_approval) had its request silently dropped — the eventual
# promotion spawn (_spawn_session(preexisting_row=True, **params/**spawn_kwargs))
# ran with client_node=None, display=None, so resolve_display_mode always returned
# "virtual". Per the controller's ruling, the fix resolves and persists the decision
# at enqueue/hold time (on THIS daemon), never by widening the shared queue params
# blob — a daemon whose _spawn_session predates a new params key would TypeError and
# the drain would kill the session (see the comment on that blob in
# _enqueue_session). Promotion picks the decision up through the existing
# `getattr(_prior, "display_mode", None) or ...` precedence already covered above.

@pytest.mark.asyncio
async def test_queued_native_request_persists_and_promotes_native(spawn_env):
    """A local operator at the capacity cap must not lose the native decision to the
    queue: it is resolved and persisted on the row/in-memory record at enqueue time,
    not deferred to a spawn that (per the shared-queue-blob constraint) never learns
    the original client_node/display."""
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native, max_active_sessions=1)
    try:
        a = await orch.create_session("one", account="acct")
        assert a.status == "idle"
        b = await orch.create_session("n", account="acct", client_node="dobby")
        assert b.status == "queued"
        assert b.display_mode == "native"
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            row = await db.get(DbSession, b.id)
        assert row.display_mode == "native"
        # The queue params JSON blob must NOT carry client_node/display (the
        # mixed-daemon-version TypeError hazard the controller ruled against).
        from server.sessions import session_queue as sq
        entry = await sq.next_entry()
        assert "client_node" not in entry.params and "display" not in entry.params

        await orch.close_session(a.id)
        started = await orch._maybe_start_queued()
        assert started == 1
        promoted = orch._records[b.id]
        assert promoted.display_mode == "native"
        assert b.id not in x11.started
        assert orch._display_env(b.id) == {"DISPLAY": ":0"}
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_held_native_request_persists_and_promotes_native(spawn_env):
    """A model-gated session parked at pending_approval must not lose the native
    decision either: resolved and persisted at hold time (same helper, same
    precedence), so a human approving it minutes or hours later — or after a daemon
    restart rebuilds the in-memory record from the row via rehydrate — still gets a
    native session, not a silent fallback to the VNC viewport."""
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session(
            "n", account="acct", client_node="dobby", model="fable")
        assert rec.status == "pending_approval"
        assert rec.display_mode == "native"
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            row = await db.get(DbSession, rec.id)
        assert row.display_mode == "native"

        out = await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
        assert out.status != "pending_approval"
        assert out.display_mode == "native"
        assert x11.started == []
        assert orch._display_env(out.id) == {"DISPLAY": ":0"}
    finally:
        await orch.stop()
