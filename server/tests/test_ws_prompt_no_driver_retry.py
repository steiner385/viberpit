"""A prompt that races a driver teardown must be retried, not dropped (2026-08-30).

Live incident: a session's driver went away after the tab attached, so
_ensure_live short-circuited on the EDGE's live_tasks membership (its stream task
was still open) while the DAEMON no longer held a driver. send_prompt returned
"no_driver" and the user's message evaporated — the UI read "Ready", the daemon
logged one WARNING, and the session looked simply unresponsive.

The bridge is the only place holding both the identity and the prompt, so it owns
the recovery: on "no_driver", tear down the stale stream entry, rebuild through
the full _ensure_live path (which calls ensure_driver), and send ONCE more. Only
a retry that also fails may surface promptRejected.
"""
from __future__ import annotations

import json
import time

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws, not conftest


def _attach(ws, session_id):
    ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
    _drain_events(ws, until_type="replayComplete")


def test_prompt_survives_a_transient_no_driver(ws_env):
    """The incident shape, at the bridge's contract boundary: the FIRST
    send_prompt answers "no_driver" (the daemon dropped the driver after this
    socket attached, so _ensure_live's live_tasks short-circuit vouched for a
    stream that no longer had one). The bridge must tear down its stale stream
    entry, rebuild via the full _ensure_live path, and resend ONCE — delivering
    the prompt with no promptRejected.

    The seam is send_prompt itself rather than reaching into orchestrator
    internals: popping _drivers mid-live leaves the orchestrator in a state
    ensure_driver never produces, and the test then hangs fighting the harness
    instead of exercising the bridge.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "race-victim")
    sid = session.id

    real_send = orch.send_prompt
    calls: list[str] = []

    async def flaky_send(session_id, text, attachment_ids=None):
        calls.append(text)
        if len(calls) == 1:
            return "no_driver"          # the race: driver vanished post-ensure
        return await real_send(session_id, text, attachment_ids)

    orch.send_prompt = flaky_send  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "please do not vanish"}))
        # Poll the spy rather than draining frames: the success path may emit
        # nothing further on this socket, and the test-client's receive blocks
        # forever on a silent socket (its drain timeout only ticks BETWEEN
        # frames). The spy is the contract: retried == 2 sends, rejected == 1.
        deadline = time.monotonic() + 8.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert len(calls) == 2, (
        "prompt was NOT retried after the transient no_driver — the incident "
        f"behavior (silent drop / promptRejected) persists; sends seen: {calls!r}")
    assert calls == ["please do not vanish"] * 2


def test_prompt_still_rejected_when_the_rebuild_also_fails(ws_env):
    """The ack contract survives: if recovery is impossible the browser still
    gets a terminal frame (promptRejected or error) so nothing is silently lost."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "hopeless")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        client.portal.call(lambda: orch._drivers.pop(sid, None))

        async def broken_ensure(session_id, identity=None, **kw):
            raise RuntimeError("rebuild impossible (test)")
        orch.ensure_driver = broken_ensure  # type: ignore[method-assign]

        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "held please"}))
        events = _drain_events(ws, until=lambda e: e.get("type") in
                               ("promptRejected", "error"), timeout=8.0)
        terminal = [e for e in events if e.get("type") in ("promptRejected", "error")]
        assert terminal, f"no rejection surfaced: {[e.get('type') for e in events]}"


def test_prompt_survives_a_failing_context_lookup(ws_env):
    """The context chip (spec §4.5) added `await orchestrator.get_session(...)` to the
    prompt frame. In fleet mode that is a real per-session RPC to the owning daemon, so
    it can raise DaemonTimeout/DaemonUnavailable — on a path that previously had no such
    dependency at all. A transient daemon hiccup must cost the user the CHIP, never the
    message.
    """
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ctx-victim")
    sid = session.id

    real_send = orch.send_prompt
    seen: list[dict] = []

    async def spy_send(session_id, text, attachment_ids=None, **kw):
        seen.append(dict(kw))
        return await real_send(session_id, text, attachment_ids, **kw)

    orch.send_prompt = spy_send  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)

        def boom(*_a, **_k):
            raise RuntimeError("daemon unavailable")

        orch.get_session = boom  # type: ignore[method-assign]
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "what is on this page?",
                                 "context": {"route": "/sessions"}}))
        deadline = time.monotonic() + 8.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)

    assert seen == [{}], (
        "a failed context lookup must degrade to 'no context', not drop the prompt; "
        f"sends seen: {seen!r}")
