"""An answerQuestion the daemon cannot resolve must not vanish silently.

The historical failure mode: ``orchestrator.answer_question`` returned False
(unknown id, identity mismatch, no driver) and the WS handler discarded the
boolean — a 200/no-op that left the user staring at a card that never reacts.
The socket now gets an error frame naming the rejection.
"""
from __future__ import annotations

import json

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401


def test_rejected_answer_surfaces_an_error_frame(ws_env):  # noqa: F811 — pytest fixture
    _app, orch, client = ws_env([])
    session = client.portal.call(
        lambda: orch.create_session("test-rejected-answer", identity="wstest@example.com")
    )
    session_id = session.id

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

# The permissionResponse sibling (PERMISSION_NOT_PENDING) is asserted in
# test_ws.py::test_unknown_permission_and_interrupt_safe, which previously
# enshrined the silent no-op.
