"""A rejected permissionResponse must log its ActionError code, and only the
403 PERMISSION_NOT_OWNED case — a real defect (#2200/#2205) — belongs at
WARNING. The 404 PERMISSION_NOT_PENDING case is an ordinary stale-card click
and must log at INFO so it stops accumulating into auto-filed error reports
(#2210). The answerQuestion sibling is covered by test_ws_rejected_answer.py.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from server.sessions.session_actions import ActionError
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401


def test_not_pending_permission_logs_at_info(ws_env, caplog):  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-perm-not-pending-log")
    session_id = session.id

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text(json.dumps({
                "type": "permissionResponse", "sessionId": session_id,
                "permId": "does-not-exist", "decision": "allow",
            }))
            events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=4.0)
            err = next((e for e in events if e.get("type") == "error"), None)
            assert err is not None and err.get("code") == "PERMISSION_NOT_PENDING"

    client.portal.call(orch.close_session, session_id)

    rejected = [r for r in caplog.records if "ws.resolve_permission rejected" in r.message]
    assert rejected, "expected a ws.resolve_permission rejected log line"
    assert rejected[0].levelno == logging.INFO
    assert "PERMISSION_NOT_PENDING" in rejected[0].message


def test_owned_elsewhere_permission_logs_at_warning(ws_env, caplog):  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-perm-not-owned-log")
    session_id = session.id

    async def _fake_resolve_permission(*_args, **_kwargs):
        raise ActionError(
            "PERMISSION_NOT_OWNED",
            "That permission request is still pending, but it was raised under a "
            "different identity than yours, so your decision was refused.",
            403,
        )

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with patch("server.bridge.ws.session_actions.resolve_permission", _fake_resolve_permission):
            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                _drain_events(ws, until_type="replayComplete")
                ws.send_text(json.dumps({
                    "type": "permissionResponse", "sessionId": session_id,
                    "permId": "owned-elsewhere", "decision": "allow",
                }))
                events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=4.0)
                err = next((e for e in events if e.get("type") == "error"), None)
                assert err is not None and err.get("code") == "PERMISSION_NOT_OWNED"

    client.portal.call(orch.close_session, session_id)

    rejected = [r for r in caplog.records if "ws.resolve_permission rejected" in r.message]
    assert rejected, "expected a ws.resolve_permission rejected log line"
    assert rejected[0].levelno == logging.WARNING
    assert "PERMISSION_NOT_OWNED" in rejected[0].message
