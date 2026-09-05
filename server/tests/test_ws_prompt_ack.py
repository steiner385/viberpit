"""The edge must ACK a prompt's receipt before it starts working on it.

A prompt frame used to produce nothing observable until the driver echoed the
user message back — which happens after ``_ensure_live`` has (possibly) woken a
detached session and after the SDK has started the turn. On a resumed session
that is 30 s–2 min of a completely silent socket, so the browser could not tell
a received prompt from one that fell on the floor.

There is already a NEGATIVE ack (``promptRejected``). These tests pin the
positive one:

  * ``promptAck stage=received|resuming`` — emitted BEFORE the slow wake-up, so
    it is the FIRST thing the browser hears back. Ordering is the whole point:
    an ack sent after ``_ensure_live`` would answer the question 90 seconds
    after it was asked.
  * ``promptAck stage=delivered`` — the driver has the text; a turn starts next.
"""
from __future__ import annotations

import json

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws


def _attach(ws, session_id):
    ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
    _drain_events(ws, until_type="replayComplete")


def _acks(frames):
    return [f for f in frames if f.get("type") == "promptAck"]


def test_prompt_is_acked_before_the_turn_produces_anything(ws_env):
    """The ack must be the FIRST frame back — ahead of the user echo."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ack-me")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "user")

    acks = _acks(frames)
    assert acks, f"no promptAck frame was ever sent; got {[f.get('type') for f in frames]!r}"

    first_ack = next(i for i, f in enumerate(frames) if f.get("type") == "promptAck")
    echoes = [i for i, f in enumerate(frames) if f.get("type") == "user"]
    assert echoes, "test did not observe the user echo it drains to"
    assert first_ack < echoes[0], (
        "promptAck arrived AFTER the turn's user echo — an ack that waits for the "
        "work it is acknowledging tells the user nothing they did not already see"
    )


def test_receipt_ack_names_the_session_and_a_receipt_stage(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ack-shape")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "user")

    receipt = _acks(frames)[0]
    assert receipt["sessionId"] == sid
    assert receipt["stage"] in ("received", "resuming"), receipt


def test_delivered_ack_follows_the_receipt_ack(ws_env):
    """Two acks, in order: the edge has it, then the driver has it."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "two-acks")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(
            ws, until=lambda f: f.get("type") == "promptAck" and f.get("stage") == "delivered")

    stages = [f["stage"] for f in _acks(frames)]
    assert stages[0] in ("received", "resuming"), stages
    assert "delivered" in stages, stages
    assert stages.index("delivered") > 0, stages


def test_a_refused_prompt_is_not_acked_as_delivered(ws_env):
    """A drain rejects the prompt. The receipt ack is still honest (we DID receive
    it), but nothing may claim it reached the driver."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "drained")
    sid = session.id

    async def refuse(session_id, text, attachment_ids=None, **kw):
        return "draining"

    orch.send_prompt = refuse  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "promptRejected")

    stages = [f["stage"] for f in _acks(frames)]
    assert "delivered" not in stages, (
        f"claimed delivery of a prompt the server refused; acks={stages!r}")
    assert any(f.get("type") == "promptRejected" for f in frames)
