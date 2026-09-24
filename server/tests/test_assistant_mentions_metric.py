"""assistant_mentions_total (spec v2 §4.10/§10.2, Task 10.2): counted in the WS prompt
path when an assistant-kind prompt embeds a `[title](/sessions/<id>)` session mention
(the AssistantSheet composer's own insert shape — see MentionMenu.tsx). A coding session
sending the same text is never counted; the metric is assistant-kind only, mirroring
assistant_prompts_total right above it in server/bridge/ws.py.
"""
from __future__ import annotations

import functools
import json

from server.obs.metrics import assistant_mentions_total
from server.sessions.types import ASSISTANT_KIND
from server.tests.test_ws import _drain_events, _IDENTITY_HEADERS, FakeSDKClient, ws_env  # noqa: F401


def test_mention_link_increments_metric_for_assistant_session(ws_env):  # noqa: F811
    _app, orch, client = ws_env([], client_cls=FakeSDKClient)
    identity = _IDENTITY_HEADERS["Tailscale-User-Login"]
    assistant = client.portal.call(functools.partial(
        orch.create_session, "assistant-mentions-ws", identity=identity,
        kind=ASSISTANT_KIND, owner_identity=identity))

    before = assistant_mentions_total.value()

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": assistant.id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({
            "type": "prompt", "sessionId": assistant.id,
            "text": "what about [fix the thing](/sessions/abc123)?",
        }))
        _drain_events(
            ws, until=lambda e: e.get("type") == "user" and "abc123" in e.get("text", ""),
            timeout=8.0)

    assert assistant_mentions_total.value() == before + 1

    client.portal.call(orch.close_session, assistant.id)


def test_plain_prompt_does_not_increment_the_metric(ws_env):  # noqa: F811
    _app, orch, client = ws_env([], client_cls=FakeSDKClient)
    identity = _IDENTITY_HEADERS["Tailscale-User-Login"]
    assistant = client.portal.call(functools.partial(
        orch.create_session, "assistant-no-mention-ws", identity=identity,
        kind=ASSISTANT_KIND, owner_identity=identity))

    before = assistant_mentions_total.value()

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": assistant.id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({
            "type": "prompt", "sessionId": assistant.id, "text": "what is running right now?",
        }))
        _drain_events(
            ws, until=lambda e: e.get("type") == "user" and e.get("text") == "what is running right now?",
            timeout=8.0)

    assert assistant_mentions_total.value() == before

    client.portal.call(orch.close_session, assistant.id)


def test_coding_session_mention_shaped_text_is_never_counted(ws_env):  # noqa: F811
    """The marker is assistant-kind gated, not text-shape gated: a coding session
    that happens to paste a `](/sessions/` link (e.g. quoting a teammate) must not
    inflate a metric that means "the assistant composer's mention affordance was used"."""
    _app, orch, client = ws_env([], client_cls=FakeSDKClient)
    coder = client.portal.call(orch.create_session, "coder-mention-shaped-ws")

    before = assistant_mentions_total.value()

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": coder.id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({
            "type": "prompt", "sessionId": coder.id,
            "text": "see [that session](/sessions/xyz789)",
        }))
        _drain_events(
            ws, until=lambda e: e.get("type") == "user" and "xyz789" in e.get("text", ""),
            timeout=8.0)

    assert assistant_mentions_total.value() == before

    client.portal.call(orch.close_session, coder.id)
