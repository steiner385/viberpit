"""A cancelQueued the daemon cannot honor must not vanish silently.

The same shadow/silent-rejection shape as ``answerQuestion`` /
``permissionResponse`` (issue #917): ``orchestrator.cancel_queued`` returns
False (index out of range because the queue already drained/shifted, or no live
driver) and the WS handler discarded the boolean — a 200/no-op that left the
user clicking "×" on a queued-prompt chip that never disappears. The socket now
gets an error frame naming the rejection.
"""
from __future__ import annotations

import json

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401


def test_rejected_cancel_queued_surfaces_an_error_frame(ws_env):  # noqa: F811 — pytest fixture
    _app, orch, client = ws_env([])
    session = client.portal.call(
        lambda: orch.create_session("test-rejected-cancel", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")
        # Nothing is queued, so any index is stale → cancel_queued returns False.
        ws.send_text(json.dumps({
            "type": "cancelQueued", "sessionId": session_id, "index": 5,
        }))
        events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=5.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None, f"no error frame in {[e.get('type') for e in events]}"
        assert err.get("code") == "QUEUED_NOT_FOUND"
        assert err.get("sessionId") == session_id

    client.portal.call(orch.close_session, session_id)
