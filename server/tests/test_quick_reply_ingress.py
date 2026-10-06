"""Learning uses accepted human prompt ingress and verified actor metadata."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request, WebSocket

from server.auth.middleware import current_identity, require_ws_identity
from server.tests.test_send_prompt_route import _ENV, _H, _make_client, _rec
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401


@pytest.mark.parametrize("source", ["typed", "suggested"])
@pytest.mark.parametrize("via", [None, "assistant", "cli", "mcp-external", "internal"])
def test_rest_observation_uses_verified_actor_and_source(source, via):

    client, fake = _make_client(_rec())
    fake.send_prompt = AsyncMock(return_value="sent")

    async def identify(request: Request):
        request.state.via = via
        return "personal@example.com"

    client.app.dependency_overrides[current_identity] = identify
    observe = AsyncMock()
    with patch.dict(os.environ, _ENV), client, patch(
        "server.api.sessions.observe_reply", observe, create=True,
    ):
        response = client.post("/api/sessions/s1/prompt", headers=_H,
                               json={"text": "please continue", "replySource": source,
                                     "identity": "forged", "via": "forged"})
    assert response.status_code == 200
    observe.assert_awaited_once_with("personal@example.com", "please continue",
                                    source=source, via=via, has_attachments=False)


@pytest.mark.parametrize("delivery", ["draining", "no_driver", "recovering", "unavailable"])
def test_rest_rejected_delivery_is_not_observed(delivery):
    client, fake = _make_client(_rec())
    fake.send_prompt = AsyncMock(return_value=delivery)
    observe = AsyncMock()
    with patch.dict(os.environ, _ENV), client, patch(
        "server.api.sessions.observe_reply", observe, create=True,
    ):
        client.post("/api/sessions/s1/prompt", headers=_H, json={"text": "continue"})
    observe.assert_not_awaited()


def test_rest_unknown_source_is_rejected():
    client, fake = _make_client(_rec())
    with patch.dict(os.environ, _ENV), client:
        response = client.post("/api/sessions/s1/prompt", headers=_H,
                               json={"text": "continue", "replySource": "untrusted"})
    assert response.status_code == 422
    assert fake.sent == []


@pytest.mark.parametrize("source,attachments,via", [
    (None, None, None), ("suggested", None, None), ("typed", ["image"], None),
    ("typed", None, "cli"), ("unknown", None, None), ({"bad": True}, None, None),
])
def test_ws_observation_metadata(ws_env, source, attachments, via):  # noqa: F811

    app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "learn")
    orch.send_prompt = AsyncMock(return_value="sent")

    async def identify(websocket: WebSocket):
        websocket.state.via = via
        return _IDENTITY_HEADERS["Tailscale-User-Login"]

    app.dependency_overrides[require_ws_identity] = identify
    observe = AsyncMock()
    with patch("server.bridge.ws.observe_reply", observe, create=True):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            frame = {"type": "prompt", "sessionId": session.id, "text": "please continue",
                     "attachmentIds": attachments, "identity": "forged", "via": "forged"}
            if source is not None:
                frame["replySource"] = source
            ws.send_json(frame)
            _drain_events(ws, until=lambda f: f.get("stage") == "delivered")
            # Attach follows the prompt on the session work chain, so its completion
            # also proves that post-ack observation has finished.
            ws.send_json({"type": "attach", "sessionId": session.id})
            _drain_events(ws, until_type="replayComplete")
    if source in (None, "typed", "suggested"):
        observe.assert_awaited_once_with(_IDENTITY_HEADERS["Tailscale-User-Login"],
                                        "please continue", source=source or "typed",
                                        via=via, has_attachments=bool(attachments))
    else:
        observe.assert_not_awaited()


@pytest.mark.parametrize("source", ["typed", "suggested"])
def test_ws_rejected_delivery_is_not_observed(ws_env, source):  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "rejected")
    orch.send_prompt = AsyncMock(return_value="draining")
    observe = AsyncMock()
    with patch("server.bridge.ws.observe_reply", observe, create=True):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_json({"type": "prompt", "sessionId": session.id, "text": "continue",
                          "replySource": source})
            frames = _drain_events(ws, until_type="promptRejected")
            assert frames[-1]["replySource"] == source
    observe.assert_not_awaited()


def test_rest_learning_failure_does_not_fail_delivery():
    client, fake = _make_client(_rec())
    fake.send_prompt = AsyncMock(return_value="sent")
    with patch.dict(os.environ, _ENV), client, patch(
        "server.api.sessions.observe_reply", AsyncMock(side_effect=RuntimeError), create=True,
    ):
        response = client.post("/api/sessions/s1/prompt", headers=_H, json={"text": "continue"})
    assert response.status_code == 200


def test_ws_learning_failure_does_not_disrupt_session_chain(ws_env):  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "optional-learning")
    orch.send_prompt = AsyncMock(return_value="sent")
    observe = AsyncMock(side_effect=RuntimeError)
    with patch("server.bridge.ws.observe_reply", observe, create=True):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_json({"type": "prompt", "sessionId": session.id, "text": "continue"})
            _drain_events(ws, until=lambda f: f.get("stage") == "delivered")
            ws.send_json({"type": "attach", "sessionId": session.id})
            _drain_events(ws, until_type="replayComplete")
    observe.assert_awaited_once()


def test_rest_default_source_and_distinct_authenticated_users():
    client, fake = _make_client(_rec())
    fake.send_prompt = AsyncMock(return_value="sent")
    observe = AsyncMock()
    with patch.dict(os.environ, _ENV), client, patch(
        "server.api.sessions.observe_reply", observe, create=True,
    ):
        for identity in ("alice@example.com", "bob@example.com"):
            response = client.post("/api/sessions/s1/prompt",
                                   headers={"Tailscale-User-Login": identity},
                                   json={"text": "continue"})
            assert response.status_code == 200
    assert [call.args[0] for call in observe.await_args_list] == [
        "alice@example.com", "bob@example.com",
    ]
    assert all(call.kwargs["source"] == "typed" for call in observe.await_args_list)


@pytest.mark.parametrize("record", [None, _rec()])
def test_rest_failed_resume_or_missing_session_is_not_observed(record):
    client, _fake = _make_client(record, resumable=False)
    observe = AsyncMock()
    with patch.dict(os.environ, _ENV), client, patch(
        "server.api.sessions.observe_reply", observe, create=True,
    ):
        response = client.post("/api/sessions/s1/prompt", headers=_H, json={"text": "continue"})
    assert response.status_code in (404, 409)
    observe.assert_not_awaited()
