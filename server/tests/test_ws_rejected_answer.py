"""An answerQuestion the daemon cannot resolve must not vanish silently.

The historical failure mode: ``orchestrator.answer_question`` returned False
(unknown id, identity mismatch, no driver) and the WS handler discarded the
boolean — a 200/no-op that left the user staring at a card that never reacts.
The socket now gets an error frame naming the rejection.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from server.sessions.session_actions import ActionError
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401


def test_rejected_answer_surfaces_an_error_frame(ws_env, caplog):  # noqa: F811 — pytest fixture
    _app, orch, client = ws_env([])
    session = client.portal.call(
        lambda: orch.create_session("test-rejected-answer", identity="wstest@example.com")
    )
    session_id = session.id

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text(json.dumps({
                "type": "answerQuestion", "sessionId": session_id,
                "questionId": "no-such-question", "selected": ["Red"],
            }))
            events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=5.0)
            err = next((e for e in events if e.get("type") == "error"), None)
            assert err is not None, f"no error frame in {[e.get('type') for e in events]}"
            assert err.get("code") == "QUESTION_NOT_PENDING"
            assert err.get("sessionId") == session_id

    client.portal.call(orch.close_session, session_id)

    # The stale-card 404 is expected user behaviour (#2210) — it must not log
    # at WARNING, and the code must be in the line so an auto-error fingerprint
    # can tell it apart from the QUESTION_NOT_OWNED defect below without a join
    # against audit_events.
    rejected = [r for r in caplog.records if "ws.answer_question rejected" in r.message]
    assert rejected, "expected a ws.answer_question rejected log line"
    assert rejected[0].levelno == logging.INFO
    assert "QUESTION_NOT_PENDING" in rejected[0].message


def test_owned_elsewhere_answer_logs_at_warning(ws_env, caplog):  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(
        lambda: orch.create_session("test-answer-not-owned", identity="wstest@example.com")
    )
    session_id = session.id

    async def _fake_answer_question(*_args, **_kwargs):
        raise ActionError(
            "QUESTION_NOT_OWNED",
            "That question is still pending, but it was asked under a different "
            "identity than yours, so this answer was refused.",
            403,
        )

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with patch("server.bridge.ws.session_actions.answer_question", _fake_answer_question):
            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                _drain_events(ws, until_type="replayComplete")
                ws.send_text(json.dumps({
                    "type": "answerQuestion", "sessionId": session_id,
                    "questionId": "owned-elsewhere", "selected": ["Red"],
                }))
                events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=5.0)
                err = next((e for e in events if e.get("type") == "error"), None)
                assert err is not None, f"no error frame in {[e.get('type') for e in events]}"
                assert err.get("code") == "QUESTION_NOT_OWNED"

    client.portal.call(orch.close_session, session_id)

    rejected = [r for r in caplog.records if "ws.answer_question rejected" in r.message]
    assert rejected, "expected a ws.answer_question rejected log line"
    assert rejected[0].levelno == logging.WARNING
    assert "QUESTION_NOT_OWNED" in rejected[0].message

# The permissionResponse sibling (PERMISSION_NOT_PENDING) is asserted in
# test_ws.py::test_unknown_permission_and_interrupt_safe, which previously
# enshrined the silent no-op.
