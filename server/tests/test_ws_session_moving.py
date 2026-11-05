"""D-17: a WS frame refused because its session is between hosts (SessionMoving).

Before the final-review fix wave, only ``attach`` caught it: a prompt fell through to
``_work_chain``'s ``log.exception`` — an ERROR (auto-filed issue) with no
``promptRejected``, so the browser's bubble said nothing and the text was gone — and an
answer/permission frame raised straight out of the receive loop. Every path now answers
with the established rejection frame carrying ``SESSION_MOVING`` and logs at INFO.
"""
from __future__ import annotations

import json
import logging

import pytest

from server.sessions.session_move import SESSION_MOVING, SessionMoving
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws


def _attach(ws, session_id):
    ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
    _drain_events(ws, until_type="replayComplete")


def _moving(session_id):
    async def _raise(*_a, **_k):
        raise SessionMoving(session_id, "rocky")
    return _raise


def _no_ws_errors(caplog):
    return [r for r in caplog.records
            if r.name.startswith("server.bridge") and r.levelno >= logging.WARNING]


def _is_rejected(e):
    return e.get("type") == "promptRejected"


def test_prompt_on_a_live_session_is_held_not_dropped(ws_env, caplog):
    """The send_prompt site: the session is attached (live), so the refusal comes from
    send_prompt itself."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        orch.send_prompt = _moving(sid)  # type: ignore[method-assign]
        caplog.set_level(logging.INFO)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "keep me", "attachmentIds": ["a1"]}))
        events = _drain_events(ws, until=_is_rejected)
    rejected = [e for e in events if _is_rejected(e)]
    assert rejected, [e.get("type") for e in events]
    assert rejected[0]["reason"] == SESSION_MOVING
    assert rejected[0]["text"] == "keep me"
    assert rejected[0]["attachmentIds"] == ["a1"]
    assert rejected[0]["movingTo"] == "rocky"
    assert not [e for e in events if e.get("type") == "promptAck"
                and e.get("stage") == "failed"]
    assert "interrupt" not in rejected[0]          # a plain prompt stays plain
    assert not _no_ws_errors(caplog)


@pytest.mark.parametrize("held", ["moving", "recovering"])
def test_a_held_interrupt_and_post_keeps_its_interrupt_flag(ws_env, held):
    """#3317: the rejection echoes ``interrupt`` so the browser's re-queued retry is
    still "interrupt and post", not a plain prompt queued behind the turn."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        if held == "moving":
            orch.send_prompt = _moving(sid)  # type: ignore[method-assign]
        else:
            async def _unavailable(*_a, **_k):
                return "recovering"
            orch.send_prompt = _unavailable  # type: ignore[method-assign]
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                 "text": "stop and do this", "interrupt": True}))
        events = _drain_events(ws, until=_is_rejected)
    rejected = [e for e in events if _is_rejected(e)]
    assert rejected, [e.get("type") for e in events]
    assert rejected[0]["interrupt"] is True
    assert rejected[0]["text"] == "stop and do this"


def test_prompt_retried_after_no_driver_is_held_when_the_retry_meets_a_move(ws_env, caplog):
    """The retry site: the first send answers no_driver, the rebuild succeeds, and the
    retried send meets the move."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    calls: list[int] = []

    async def first_no_driver_then_moving(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            return "no_driver"
        raise SessionMoving(sid, "rocky")

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        orch.send_prompt = first_no_driver_then_moving  # type: ignore[method-assign]
        caplog.set_level(logging.INFO)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "again"}))
        events = _drain_events(ws, until=_is_rejected)
    rejected = [e for e in events if _is_rejected(e)]
    assert len(calls) == 2
    assert rejected and rejected[0]["reason"] == SESSION_MOVING
    assert rejected[0]["text"] == "again"
    assert not _no_ws_errors(caplog)


def test_prompt_whose_wake_meets_a_move_is_held_not_failed(ws_env, caplog):
    """The wake site: the session is not live, so the refusal comes from ensure_driver.
    That used to end as a terminal ``promptAck failed``; a move is a hold."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    orch.ensure_driver = _moving(sid)  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "wake me"}))
        events = _drain_events(ws, until=_is_rejected)
    rejected = [e for e in events if _is_rejected(e)]
    assert rejected and rejected[0]["reason"] == SESSION_MOVING
    assert rejected[0]["text"] == "wake me"
    assert not [e for e in events if e.get("type") == "promptAck"
                and e.get("stage") == "failed"]
    assert not _no_ws_errors(caplog)


@pytest.mark.parametrize("frame,orch_attr,echo", [
    ({"type": "answerQuestion", "questionId": "q-1", "selected": ["yes"]},
     "answer_question", {"questionId": "q-1"}),
    ({"type": "permissionResponse", "permId": "p-1", "decision": "allow"},
     "resolve_permission", {"permId": "p-1"}),
])
def test_answer_and_permission_during_a_move_get_the_moving_error(
        ws_env, caplog, frame, orch_attr, echo):
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    setattr(orch, orch_attr, _moving(sid))
    caplog.set_level(logging.INFO)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({**frame, "sessionId": sid}))
        events = _drain_events(ws, until=lambda e: e.get("type") == "error")
        # The socket survived the refusal: it still answers.
        ws.send_text(json.dumps({"type": "nope"}))
        after = _drain_events(ws, until=lambda e: e.get("code") == "UNKNOWN_TYPE")
    errors = [e for e in events if e.get("type") == "error"]
    assert errors and errors[0]["code"] == SESSION_MOVING
    assert errors[0]["message"] == "Moving to rocky…"
    assert errors[0]["sessionId"] == sid
    assert {k: errors[0][k] for k in echo} == echo
    assert any(e.get("code") == "UNKNOWN_TYPE" for e in after)
    assert not _no_ws_errors(caplog)


def test_other_chained_frames_during_a_move_get_the_moving_error(ws_env, caplog):
    """The chain's fallback: interrupt (now in _MOVE_BLOCKED) and every other chained
    frame answer SESSION_MOVING at INFO, never the chain's log.exception."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mover").id
    orch.interrupt = _moving(sid)  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "interrupt", "sessionId": sid}))
        events = _drain_events(ws, until=lambda e: e.get("type") == "error")
    errors = [e for e in events if e.get("type") == "error"]
    assert errors and errors[0]["code"] == SESSION_MOVING
    assert not _no_ws_errors(caplog)
    assert not [r for r in caplog.records if "ws.work failed" in r.getMessage()]
