"""A WS attach that lands WHILE a session is spawning must open a live stream.

Incident 2026-09-04 00:50 EDT (rocky, session bcbdf359): a Fable session parked
``pending_approval``; the user opened it (read-only detached attach — no driver
yet) and clicked Approve. The spawn took 2.7 s. ``_spawn_session`` pre-seeds the
in-memory record with ``status="idle"`` BEFORE ``driver.start()`` and only
registers the driver in ``_drivers`` afterwards, so for those 2.7 s the record
read as "spawned but detached". The session view's 5-second poll saw the status
leave ``pending_approval`` and re-attached exactly in that window; the bridge
derived ``detached=True`` (no driver yet), took the read-only path again, and
never popped the deferred initial prompt. Once the driver landed, nothing else
re-attached — the prompt sat staged until the user navigated away and back.

Two holes, both in the orchestrator's view of a spawning session:

1. ``detached`` was derived purely from ``_drivers`` membership, so a mid-spawn
   session was indistinguishable from a finished one. An in-flight spawn is now
   tracked (``_spawning``); the record reads live and ``ensure_driver`` WAITS for
   the spawn instead of racing it with a second driver.
2. ``resolve_model_approval`` / ``_maybe_start_queued`` popped the in-memory
   record before calling ``_spawn_session``, which re-seeds it hundreds of ms
   later (worktree checkout, config compose, X display). A list poll in that gap
   saw the session VANISH, and the view's status-transition re-attach keys off a
   status it never observed. The record now stays in place; the pre-seed
   overwrites it.
"""
from __future__ import annotations

import asyncio
import functools
import json
import time

from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_session_queue_drain import _make_orch as _make_queue_orch
from server.tests.test_session_queue_drain import _stop_orch as _stop_queue_orch
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws

_IDENTITY = "wstest@example.com"


def _gated_client_cls():
    """A FakeSDKClient whose connect (``__aenter__``) blocks on a gate the test
    opens — holds ``driver.start()``, and so the whole spawn, mid-flight."""

    class GatedClient(FakeSDKClient):
        gate: asyncio.Event | None = None
        entered = 0

        async def __aenter__(self):
            cls = type(self)
            if cls.gate is None:
                cls.gate = asyncio.Event()
            await cls.gate.wait()
            cls.entered += 1
            return await super().__aenter__()

    return GatedClient


def _wait_until(pred, *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pred(), f"timed out waiting for {what}"


def _hold(ws_env, client_cls=None):
    """Create a gated (fable) session; return (orch, client, session_id)."""
    msgs = [f.sys_init("sess-fable"), f.result(session_id="sess-fable", uuid="t1")]
    _app, orch, client = ws_env(msgs, client_cls=client_cls)
    rec = client.portal.call(
        lambda: orch.create_session("fable-hold", initial_prompt="Hello!", model="fable"))
    assert rec.status == "pending_approval", rec.status
    return orch, client, rec.id


def _approve_in_background(orch, client, sid):
    return client.portal.start_task_soon(
        functools.partial(orch.resolve_model_approval, sid, "approve", identity=_IDENTITY))


def _release(cls, client, *futures) -> None:
    """Open the gate and let the held spawn finish — ALWAYS, even after a failed
    assertion, or the blocked spawn wedges the TestClient's lifespan teardown."""
    if cls.gate is not None:
        client.portal.call(cls.gate.set)
    for fut in futures:
        try:
            fut.result(timeout=10)
        except Exception:
            pass


def test_attach_during_spawn_opens_a_live_stream_and_sends_the_initial_prompt(ws_env):
    """The incident shape: attach while pending, approve, and re-attach while the
    spawn is still in ``driver.start()``. The re-attach must be a LIVE attach
    (wait for the driver) so the deferred initial prompt streams — not a second
    read-only replay that orphans it."""
    cls = _gated_client_cls()
    orch, client, sid = _hold(ws_env, cls)
    approval = None
    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            _drain_events(ws, until_type="replayComplete")      # pre-spawn, read-only

            approval = _approve_in_background(orch, client, sid)
            _wait_until(lambda: cls.gate is not None, what="spawn to reach driver.start()")

            # Mid-spawn: the record has left pending_approval but no driver is registered.
            rec = client.portal.call(lambda: orch.get_session(sid))
            assert rec is not None and rec.status == "idle", rec
            assert rec.detached is False, (
                "a spawning session read as detached — the bridge will take the "
                "read-only path and never pop the deferred prompt")

            # The view's status-transition re-attach lands here.
            ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            _release(cls, client, approval)
            events = _drain_events(ws, until_type="user", timeout=10.0)
    finally:
        _release(cls, client, *([approval] if approval else []))

    user_texts = [e.get("text") for e in events if e.get("type") == "user"]
    assert "Hello!" in user_texts, f"initial prompt never streamed; got {user_texts!r}"
    driver = orch.driver(sid)
    assert driver is not None and driver._client.sent == ["Hello!"]
    assert cls.entered == 1, "the attach built a second driver instead of waiting"


def test_ensure_driver_waits_for_an_inflight_spawn_instead_of_building_a_second_driver(ws_env):
    """``ensure_driver`` on a spawning session must await the spawn and return ITS
    driver. Before, it saw no driver + an idle record and built a replacement with
    resume=None — a second SDK process on a brand-new conversation, then silently
    overwritten by the spawn's own registration."""
    cls = _gated_client_cls()
    orch, client, sid = _hold(ws_env, cls)
    approval = _approve_in_background(orch, client, sid)
    ensure = None
    try:
        _wait_until(lambda: cls.gate is not None, what="spawn to reach driver.start()")
        ensure = client.portal.start_task_soon(
            functools.partial(orch.ensure_driver, sid, _IDENTITY))
        time.sleep(0.2)
        assert not ensure.done(), "ensure_driver returned before the spawn finished"
    finally:
        _release(cls, client, approval, *([ensure] if ensure else []))
    drv = ensure.result(timeout=10)

    assert drv is orch.driver(sid)
    assert cls.entered == 1, f"{cls.entered} SDK clients connected for one session"


def test_approval_keeps_the_record_visible_while_the_spawn_starts(ws_env):
    """Between the approval's placeholder cleanup and ``_spawn_session``'s pre-seed
    the session must not vanish from ``get_session`` / ``list_sessions``: a list
    poll in that gap drops the record, and the view's re-attach never fires."""
    orch, client, sid = _hold(ws_env)
    seen: list = []
    original = orch._effective_repo_root

    async def spy():
        # First await inside _spawn_session — inside the gap.
        seen.append(orch.get_session(sid))
        return await original()

    orch._effective_repo_root = spy  # type: ignore[method-assign]
    client.portal.call(
        lambda: orch.resolve_model_approval(sid, "approve", identity=_IDENTITY))

    assert seen, "spawn never ran"
    assert seen[0] is not None, "session vanished from the in-memory records mid-approval"
    assert seen[0].status == "pending_approval"
    assert orch.driver(sid) is not None


def test_queue_promotion_keeps_the_record_visible_while_the_spawn_starts(tmp_path):
    """Same gap on the queue path: ``_maybe_start_queued`` popped the queued record
    before ``_spawn_session`` re-seeded it."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'q.db'}"
        orch = await _make_queue_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two", initial_prompt="Hi")
            assert orch._records[b.id].status == "queued"
            seen: list = []
            original = orch._effective_repo_root

            async def spy():
                seen.append(orch.get_session(b.id))
                return await original()

            orch._effective_repo_root = spy  # type: ignore[method-assign]
            await orch.close_session(a.id)
            await orch._maybe_start_queued()
            assert seen, "queued session never spawned"
            assert seen[0] is not None, "queued session vanished from the records mid-promotion"
            assert seen[0].status == "queued"
            assert orch._records[b.id].status != "queued"
        finally:
            await _stop_queue_orch(orch)

    asyncio.run(run())
