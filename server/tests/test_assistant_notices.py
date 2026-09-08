"""Task 8.4 (spec v2 §4.7 Pack N): notice delivery — assistant channel → drawer
event + push.

Three behaviors:
  1. ``notify_event`` routes to an assistant-channel rule, calls
     ``orch.post_notice`` once, and disables the rule when ``once=True``.
  2. ``SessionOrchestrator.post_notice`` emits live (driver present) or queues
     (no driver) and reports which happened.
  3. A notice queued while the drawer was closed flushes on the next WS attach
     (mirrors the deferred-initial-prompt WS harness in test_ws_commands.py).
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import dispose_db, init_db
from server.sessions import events as ev
from server.sessions.types import ASSISTANT_KIND
from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch

_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# --------------------------------------------------------------------------- #
# 1. notify_event -> orch.post_notice, once-rule disable                      #
# --------------------------------------------------------------------------- #

class _FakeAssistantOrch:
    """Minimal stand-in for SessionOrchestrator's post_notice call surface."""

    def __init__(self, *, owner_identity: str, session_id: str = "assistant-1"):
        self.owner_identity = owner_identity
        self.session_id = session_id
        self.calls: list[tuple[str, dict]] = []
        self.delivered = True

    def get_session(self, session_id):
        if session_id != self.session_id:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(id=session_id, owner_identity=self.owner_identity)

    async def post_notice(self, identity: str, notice: dict) -> bool:
        self.calls.append((identity, notice))
        return self.delivered


async def _make_assistant_channel_rule(*, once: bool, owner_session_id: str,
                                       event_class: str = "needs_input"):
    from server.db import db_session
    from server.db.models import NotificationChannel, NotificationRule

    async with db_session() as db:
        chan = NotificationChannel(name="assistant:u@x", kind="assistant",
                                   url="assistant:deadbeef", enabled=True)
        db.add(chan)
        await db.flush()
        rule = NotificationRule(
            enabled=True, event_class=event_class, action="channel",
            channel_id=chan.id, once=once, owner_session_id=owner_session_id,
        )
        db.add(rule)
        await db.commit()
        return chan.id, rule.id


def test_notify_event_routes_to_assistant_channel_and_disables_once_rule(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'notify.db'}"
        await init_db(db_url=db_url)
        try:
            chan_id, rule_id = await _make_assistant_channel_rule(
                once=True, owner_session_id="assistant-1")
            fake_orch = _FakeAssistantOrch(owner_identity="u@x")

            from server.sessions import push as push_mod
            await push_mod.notify_event(
                "needs_input", "ccswitch needs you", "session x needs input",
                orch=fake_orch)

            assert len(fake_orch.calls) == 1
            identity, notice = fake_orch.calls[0]
            assert identity == "u@x"
            assert notice["kind"] == "needs_input"
            assert notice["title"] == "ccswitch needs you"
            assert notice["body"] == "session x needs input"
            assert notice["watchId"] == rule_id

            from sqlalchemy import select
            from server.db import db_session
            from server.db.models import NotificationRule
            async with db_session() as db:
                row = (await db.execute(
                    select(NotificationRule).where(NotificationRule.id == rule_id)
                )).scalar_one()
                assert row.enabled is False
        finally:
            await dispose_db()

    asyncio.run(run())


def test_notify_event_leaves_a_non_once_assistant_rule_enabled(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'notify2.db'}"
        await init_db(db_url=db_url)
        try:
            chan_id, rule_id = await _make_assistant_channel_rule(
                once=False, owner_session_id="assistant-1")
            fake_orch = _FakeAssistantOrch(owner_identity="u@x")

            from server.sessions import push as push_mod
            await push_mod.notify_event(
                "needs_input", "t", "b", orch=fake_orch)

            assert len(fake_orch.calls) == 1

            from sqlalchemy import select
            from server.db import db_session
            from server.db.models import NotificationRule
            async with db_session() as db:
                row = (await db.execute(
                    select(NotificationRule).where(NotificationRule.id == rule_id)
                )).scalar_one()
                assert row.enabled is True
        finally:
            await dispose_db()

    asyncio.run(run())


def test_notify_event_assistant_channel_without_orch_fails_open_no_raise(tmp_path):
    """orch=None (e.g. AutomationContext.emit has no orchestrator reference) must
    never raise — web push (already unconditional) is the only delivery."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'notify3.db'}"
        await init_db(db_url=db_url)
        try:
            await _make_assistant_channel_rule(once=True, owner_session_id="assistant-1")
            from server.sessions import push as push_mod
            await push_mod.notify_event("needs_input", "t", "b")  # orch defaults to None
        finally:
            await dispose_db()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 2. SessionOrchestrator.post_notice — live emit vs queue                     #
# --------------------------------------------------------------------------- #

def test_post_notice_emits_live_when_driver_present(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'live.db'}",
                                tmp_path=tmp_path)
        try:
            a = await orch.create_session(name="Assistant", cwd=str(tmp_path),
                                          identity="u@x", kind=ASSISTANT_KIND,
                                          owner_identity="u@x")
            assert a.id in orch._drivers, "assistant session spawns a live driver"
            driver = orch._drivers[a.id]
            stream = driver.open_stream()
            try:
                delivered = await orch.post_notice("u@x", {
                    "kind": "pr_broke", "title": "PR broke", "body": "CI failing",
                    "url": "/session/x", "watchId": "rule-1",
                })
                assert delivered is True
                got = await asyncio.wait_for(stream._queue.get(), timeout=2)
                assert isinstance(got, ev.Notice)
                assert got.kind == "pr_broke"
                assert got.title == "PR broke"
                assert got.body == "CI failing"
                assert got.url == "/session/x"
                assert got.watch_id == "rule-1"
                wire = got.to_wire()
                assert wire == {
                    "type": "notice", "kind": "pr_broke", "title": "PR broke",
                    "body": "CI failing", "url": "/session/x", "watchId": "rule-1",
                    "ts": wire["ts"],
                }
            finally:
                stream.close()
            # No pending notice was queued — it went out live.
            assert orch.pop_pending_notices(a.id) == []
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_post_notice_queues_when_no_live_driver(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}",
                                tmp_path=tmp_path)
        try:
            a = await orch.create_session(name="Assistant", cwd=str(tmp_path),
                                          identity="u@x", kind=ASSISTANT_KIND,
                                          owner_identity="u@x")
            # Simulate a detached drawer: no live driver for this identity's thread.
            orch._drivers.pop(a.id, None)

            delivered = await orch.post_notice("u@x", {
                "kind": "automation_error", "title": "Backlog runner failed",
                "body": "boom", "url": None, "watchId": "rule-2",
            })
            assert delivered is False

            pending = orch.pop_pending_notices(a.id)
            assert len(pending) == 1
            assert pending[0]["title"] == "Backlog runner failed"
            # Popped once — a second pop is empty (matches pop_pending_initial_prompt).
            assert orch.pop_pending_notices(a.id) == []
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_post_notice_returns_false_for_unknown_identity(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'unknown.db'}",
                                tmp_path=tmp_path)
        try:
            delivered = await orch.post_notice("nobody@x", {
                "kind": "pr_broke", "title": "t", "body": "b", "url": None, "watchId": None,
            })
            assert delivered is False
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 3. Pending notices flush on WS attach                                       #
# --------------------------------------------------------------------------- #

class _NoticeFlushFakeOrch:
    """Minimal WS-bridge-facing fake orchestrator (mirrors test_ws_commands.FakeOrch),
    seeded with a pending notice for the session under test."""

    def __init__(self, pending: list[dict]):
        self._pending = {"s1": list(pending)}
        self._drivers: dict = {}

    async def rehydrate(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def subscribe_events(self):
        return asyncio.Queue()

    def unsubscribe_events(self, q):
        pass

    async def ensure_driver(self, session_id, identity):
        class _Stream:
            turn_active = False
            turn_started_at = None
            async def events(self):
                if False:
                    yield None
                await asyncio.Event().wait()
            def close(self): pass

        class _Driver:
            def open_stream(self): return _Stream()
        return _Driver()

    def get_session(self, session_id):
        return None

    def transcript_path(self, session_id):
        return None

    @property
    def _registry(self):
        class _Reg:
            def pending(self, sid):
                return []
        return _Reg()

    def pending_questions(self, session_id):
        return []

    def pop_pending_initial_prompt(self, session_id):
        return None

    def pop_pending_initial_attachment_ids(self, session_id):
        return None

    async def pop_pending_notices(self, session_id):
        # Async to match the facade contract every real orchestrator wrapper uses
        # (server/sessions/facade.py, remote.py) — ws.py always `await`s this call
        # (mirrors pop_pending_initial_prompt/pop_pending_initial_attachment_ids).
        return self._pending.pop(session_id, [])


def _drain_until(ws, *, until_type: str, timeout: float = 5.0) -> list[dict]:
    """Collect frames until one of type ``until_type`` is seen, flattening
    ``batch`` frames (session-scoped events — Notice included — ride the out_q
    batcher, unlike the top-level ``commands``/``commandResult`` frames; mirrors
    test_initial_prompt_visible.py's _drain_events)."""
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        if msg.get("type") == "batch":
            frames.extend(msg.get("events", []))
        else:
            frames.append(msg)
        if any(f.get("type") == until_type for f in frames):
            return frames
    return frames


def test_pending_notice_flushes_on_ws_attach():
    orch = _NoticeFlushFakeOrch(pending=[{
        "kind": "pr_broke", "title": "PR broke", "body": "CI failing",
        "url": "/session/x", "watchId": "rule-1",
    }])
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            frames = _drain_until(ws, until_type="notice")

    types = [f.get("type") for f in frames]
    assert "notice" in types, f"expected a 'notice' frame on attach; got {types}"
    frame = next(f for f in frames if f.get("type") == "notice")
    assert frame["sessionId"] == "s1"
    assert frame["kind"] == "pr_broke"
    assert frame["title"] == "PR broke"
    assert frame["body"] == "CI failing"
    assert frame["url"] == "/session/x"
    assert frame["watchId"] == "rule-1"
    # Popped once — reattaching finds nothing left to flush.
    assert orch._pending == {}
