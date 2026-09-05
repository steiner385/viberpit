"""The lazy session resume must not freeze the socket's receive loop (#1180).

`_ensure_live` resumes a detached session's `claude` process — anecdotally the bulk
of the 30 s–2 min a user waits after hitting Enter. It used to run INLINE in the
handler's receive loop, and that loop processes frames strictly sequentially: while
a resume ran, *nothing else on that socket was handled*. No permission answer, no
question answer, no prompt for a different session.

The fix keeps ordering exactly where it is load-bearing — per session — and nowhere
else. These tests pin both halves:

  - the loop keeps serving other frames while a resume is stuck (the bug), and
  - prompts, and an interrupt following one, still run in the order they were sent
    (the thing a naive "spawn a task per frame" would have broken).

Plus the properties the deferral itself introduces or must not break: #1179's
receipt ack still landing while the chain is busy, an explicit depth cap replacing
the blocking loop's implicit TCP backpressure, and a teardown that still delivers a
prompt whose socket went away mid-resume (the blocking loop could not even observe
the disconnect until the resume returned, so it always did).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from server.bridge.ws import _MAX_SESSION_WORK
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws, not conftest


def _gate_the_resume(orch) -> tuple[threading.Event, threading.Event]:
    """Wedge ``ensure_driver`` until released. Returns (started, release).

    A threading.Event, not an asyncio one: the test body runs on the pytest thread
    while the handler runs on the TestClient's portal loop, and a threading.Event
    is the only handshake that is safe to set from either side. The wrapper polls
    it from a coroutine, so the event loop itself is never blocked — the whole
    point is to prove the loop stays responsive.
    """
    started = threading.Event()
    release = threading.Event()
    real_ensure = orch.ensure_driver

    async def slow_ensure(session_id, identity=None, **kw):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return await real_ensure(session_id, identity or "wstest@example.com")

    orch.ensure_driver = slow_ensure  # type: ignore[method-assign]
    return started, release


def _spy_sends(orch) -> list[str]:
    """Record the text of every delivered prompt, in delivery order."""
    sent: list[str] = []
    real_send = orch.send_prompt

    async def spy_send(session_id, text, attachment_ids=None, **kw):
        sent.append(text)
        return await real_send(session_id, text, attachment_ids, **kw)

    orch.send_prompt = spy_send  # type: ignore[method-assign]
    return sent


def _collect_acks(ws, want: int, timeout: float = 8.0) -> list[dict]:
    """Read frames until *want* promptAck frames have been seen.

    Not `_drain_events`: its predicate is re-evaluated over the whole accumulated
    list each round, so it cannot count occurrences. Only call this when the acks
    are already guaranteed to have been emitted — `receive_text` has no timeout of
    its own and blocks forever on a silent socket.
    """
    acks: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(acks) < want and time.monotonic() < deadline:
        try:
            msg = json.loads(ws.receive_text())
        except Exception:
            break
        frames = msg.get("events", []) if msg.get("type") == "batch" else [msg]
        acks.extend(f for f in frames if f.get("type") == "promptAck")
    return acks


def _wait_for(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_another_frame_is_answered_while_a_resume_is_still_running(ws_env):
    """The bug itself. A prompt wedges the lazy resume; a permissionResponse sent
    afterwards must still be answered — WHILE the resume is provably still stuck.

    permissionResponse is the probe because it is exactly the frame class the stall
    hurt most (an answer to a turn that is already blocked) and because an unknown
    permId produces an observable frame with no driver involvement at all.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "slow-waker")
    sid = session.id
    started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                     "text": "wake up please"}))
            assert started.wait(8.0), "the gated resume never started"

            ws.send_text(json.dumps({"type": "permissionResponse", "sessionId": sid,
                                     "permId": "no-such-permission", "decision": "allow"}))
            events = _drain_events(
                ws, until=lambda e: e.get("code") == "PERMISSION_NOT_PENDING",
                timeout=8.0)

            assert not release.is_set(), "test bug: the resume was released early"
            assert any(e.get("code") == "PERMISSION_NOT_PENDING" for e in events), (
                "the receive loop is still blocked on the lazy resume — a frame sent "
                "during a wake-up went unanswered; saw "
                f"{[e.get('type') for e in events]}")
    finally:
        release.set()


def test_the_receipt_ack_still_lands_while_the_session_chain_is_busy(ws_env):
    """#1179's guarantee must survive the deferral.

    ``promptAck stage=received|resuming`` is emitted from the receive loop, not
    from the work chain, so it arrives even when this session already has a slow
    prompt in flight ahead of it. Queueing the ack alongside the work would have
    put "I heard you" behind the very wait it exists to explain — the one frame
    that must never be subject to head-of-line blocking.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "acked-anyway")
    sid = session.id
    started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "first"}))
            assert started.wait(8.0), "the gated resume never started"
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "second"}))

            acks = _collect_acks(ws, want=2)
            assert not release.is_set(), "test bug: the resume was released early"
            assert len(acks) >= 2, (
                "the second prompt was not acked while the first was still waking "
                f"the session; acks={acks!r}")
            assert all(a["stage"] in ("received", "resuming") for a in acks), acks
            assert all(a["sessionId"] == sid for a in acks), acks
    finally:
        release.set()


def test_prompts_stay_ordered_across_a_deferred_resume(ws_env):
    """Deferral must not become a race. A second prompt sent while the first is
    still waking the session has to land SECOND — the ordering guarantee a bare
    task-per-frame would have thrown away."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ordered")
    sid = session.id
    sent = _spy_sends(orch)
    started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "first"}))
            assert started.wait(8.0), "the gated resume never started"
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "second"}))
            release.set()
            assert _wait_for(lambda: len(sent) >= 2), f"only {sent!r} was delivered"
    finally:
        release.set()

    assert sent == ["first", "second"], (
        f"prompts were reordered by the deferral: {sent!r}")


def test_an_interrupt_still_lands_after_the_prompt_it_follows(ws_env):
    """Deliberate: an interrupt does NOT overtake an undelivered prompt.

    It is tempting to let Stop skip the queue, but an interrupt against a session
    that is not live yet no-ops — the prompt would then be delivered anyway and run
    unstopped, which is strictly worse than the blocking loop it replaces (there,
    Stop arrived late but did stop the turn). Cancelling an in-flight prompt is a
    separate, client-visible contract; until it exists, order is preserved.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "stopper")
    sid = session.id

    order: list[str] = []
    real_send = orch.send_prompt
    real_interrupt = orch.interrupt

    async def spy_send(session_id, text, attachment_ids=None, **kw):
        order.append("prompt")
        return await real_send(session_id, text, attachment_ids, **kw)

    async def spy_interrupt(session_id, *a, **kw):
        order.append("interrupt")
        return await real_interrupt(session_id, *a, **kw)

    orch.send_prompt = spy_send        # type: ignore[method-assign]
    orch.interrupt = spy_interrupt     # type: ignore[method-assign]
    started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "go"}))
            assert started.wait(8.0), "the gated resume never started"
            ws.send_text(json.dumps({"type": "interrupt", "sessionId": sid}))
            release.set()
            assert _wait_for(lambda: order == ["prompt", "interrupt"]), (
                f"interrupt/prompt order broke: {order!r}")
    finally:
        release.set()


def test_a_saturated_session_chain_refuses_a_prompt_instead_of_growing(ws_env):
    """The blocking loop had implicit backpressure — a client could not outrun a
    handler it was waiting on. Deferring removes it, so the chain is explicitly
    capped, and an over-cap prompt comes back as promptRejected (which the browser
    re-queues) rather than being silently dropped or piling up unbounded."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "flooded")
    sid = session.id
    _started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            for i in range(_MAX_SESSION_WORK + 10):
                ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                         "text": f"flood {i}"}))
            events = _drain_events(
                ws, until=lambda e: (e.get("type") == "promptRejected"
                                     and e.get("reason") == "busy"),
                timeout=8.0)
            assert any(e.get("type") == "promptRejected" and e.get("reason") == "busy"
                       for e in events), (
                "an over-cap prompt was neither delivered nor refused; saw "
                f"{[e.get('type') for e in events]}")
    finally:
        release.set()


def test_a_refused_attach_still_clears_the_client_loading_state(ws_env):
    """Every aborted attach owes the client a replayComplete.

    `_resume_driver_or_error` pays that debt on all three of its failure paths —
    without it the browser sits on "Loading session…" forever. The saturated-chain
    refusal is a NEW way for an attach to abort, so it inherits the same obligation
    rather than becoming the one exception.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "flooded-attach")
    sid = session.id
    _started, release = _gate_the_resume(orch)

    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            for i in range(_MAX_SESSION_WORK + 10):
                ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                         "text": f"flood {i}"}))
            ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            events = _drain_events(
                ws, until=lambda e: e.get("type") == "replayComplete", timeout=8.0)
            assert any(e.get("code") == "SESSION_BUSY" for e in events), (
                f"no refusal surfaced; saw {[e.get('type') for e in events]}")
            assert any(e.get("type") == "replayComplete" for e in events), (
                "a refused attach left the client spinning on 'Loading session…'; "
                f"saw {[e.get('type') for e in events]}")
    finally:
        release.set()


def test_a_disconnect_during_a_resume_still_delivers_the_prompt(ws_env):
    """Teardown drains, it does not cancel.

    The blocking loop could not observe the disconnect until `_ensure_live`
    returned, so a prompt sent just before the tab closed always completed its
    resume and landed. Cancelling the chain on close would lose messages that used
    to arrive — so the socket's teardown waits the in-flight work out.

    The disconnect is delivered with ``ws.close()`` INSIDE the context manager
    rather than by leaving it. Exiting the block is not a disconnect: starlette's
    WebSocketTestSession sends the close and then immediately cancels the ASGI task's
    scope, so the handler is hard-cancelled before it can even read the frame. A real
    uvicorn client disconnect is the graceful shape modelled here — a
    ``websocket.disconnect`` message that surfaces as WebSocketDisconnect out of
    receive_text, leaving the handler free to run its teardown.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "tab-closer")
    sid = session.id
    sent = _spy_sends(orch)

    started = threading.Event()
    real_ensure = orch.ensure_driver

    async def dawdling_ensure(session_id, identity=None, **kw):
        started.set()
        await asyncio.sleep(0.75)
        return await real_ensure(session_id, identity or "wstest@example.com")

    orch.ensure_driver = dawdling_ensure  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "survive the tab close"}))
        assert started.wait(8.0), "the slow resume never started"
        ws.close(1000)   # the tab goes away while the resume is still running
        assert _wait_for(lambda: sent == ["survive the tab close"]), (
            f"a prompt in flight when the socket closed was dropped: {sent!r}")


def test_prompt_delivery_records_the_resume_versus_send_split(ws_env):
    """The measurement the issue asked for: `ws.prompt.deliver` isolates the wake
    (`ensure_ms`) from everything after the driver exists (`send_ms`), so "does the
    resume dominate the wait?" is answerable from the journal instead of by feel."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "measured")
    sid = session.id
    sent = _spy_sends(orch)

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    ws_log = logging.getLogger("server.bridge.ws")
    handler = _Collector()
    previous_level = ws_log.level
    ws_log.addHandler(handler)
    ws_log.setLevel(logging.INFO)
    try:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                     "text": "how long did that take?"}))
            assert _wait_for(lambda: bool(sent)), "the prompt was never delivered"
            assert _wait_for(lambda: any(
                getattr(r, "event", None) == "ws.prompt.deliver" for r in records))
    finally:
        ws_log.removeHandler(handler)
        ws_log.setLevel(previous_level)

    delivered = [r for r in records if getattr(r, "event", None) == "ws.prompt.deliver"]
    fields = delivered[0].fields  # type: ignore[attr-defined]
    assert {"ensure_ms", "send_ms", "status"} <= set(fields), fields
    assert fields["ensure_ms"] >= 0 and fields["send_ms"] >= 0

    resumes = [r for r in records if getattr(r, "event", None) == "ws.session.resume"]
    assert resumes, "the lazy resume itself was not timed"
    assert {"resume_ms", "total_ms", "ok"} <= set(resumes[0].fields)  # type: ignore[attr-defined]
