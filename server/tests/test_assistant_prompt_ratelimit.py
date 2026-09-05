"""Per-identity assistant prompt rate limit (spec §4.6, plan Task 3.4).

The WS ``prompt`` handler caps assistant-kind sessions at
``CCSWITCH_ASSISTANT_MAX_PROMPTS_PER_HOUR`` per identity (a token bucket with
capacity = max/6, refilling to max/3600 per second). A coding session is never
rate-limited here — the tool-call limiter in server/assistant/client.py is a
separate, per-session budget.
"""
from __future__ import annotations

import functools
import json

from server.sessions.types import ASSISTANT_KIND
from server.tests.test_ws import _drain_events, _IDENTITY_HEADERS, FakeSDKClient, ws_env  # noqa: F401


def test_assistant_prompt_cap_gates_assistant_not_coding(ws_env, monkeypatch):  # noqa: F811 — pytest fixture
    """capacity + 1 prompts to an assistant session: the last is promptRejected
    with reason RATE_LIMITED. A coding session sharing the same socket and
    identity sends more prompts than that tiny cap and is never rejected —
    the WS-level limiter only gates assistant records (spec §4.6)."""
    # capacity = max/6 = 1 → the very next assistant prompt in the same instant
    # is rejected.
    monkeypatch.setenv("CCSWITCH_ASSISTANT_MAX_PROMPTS_PER_HOUR", "6")

    _app, orch, client = ws_env([], client_cls=FakeSDKClient)
    identity = _IDENTITY_HEADERS["Tailscale-User-Login"]
    assistant = client.portal.call(functools.partial(
        orch.create_session, "assistant-ratelimit-ws", identity=identity,
        kind=ASSISTANT_KIND, owner_identity=identity))
    coder = client.portal.call(orch.create_session, "coder-ratelimit-ws")

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        # Assistant session first (mirrors test_ws.py's context test): 1st prompt
        # goes through, 2nd (capacity=1) is rejected.
        ws.send_text(json.dumps({"type": "attach", "sessionId": assistant.id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": assistant.id, "text": "one"}))
        first = _drain_events(
            ws, until=lambda e: e.get("type") == "user" and e.get("text") == "one",
            timeout=8.0)
        assert not any(e.get("type") == "promptRejected" for e in first), first

        ws.send_text(json.dumps({"type": "prompt", "sessionId": assistant.id, "text": "two"}))
        second = _drain_events(ws, until_type="promptRejected", timeout=8.0)
        rejected = [e for e in second if e.get("type") == "promptRejected"]
        assert rejected, [e.get("type") for e in second]
        assert rejected[-1]["reason"] == "RATE_LIMITED", rejected[-1]
        assert rejected[-1]["text"] == "two", rejected[-1]

        # Coding session sharing the same socket/identity: never gated here, even
        # past the point where the assistant's tiny cap already tripped.
        ws.send_text(json.dumps({"type": "attach", "sessionId": coder.id}))
        _drain_events(ws, until_type="replayComplete")
        ws.send_text(json.dumps({"type": "prompt", "sessionId": coder.id, "text": "hi"}))
        coder_events = _drain_events(
            ws, until=lambda e: e.get("type") == "user" and e.get("text") == "hi",
            timeout=8.0)
        assert not any(e.get("type") == "promptRejected" for e in coder_events), coder_events

    coder_sdk = orch._drivers[coder.id]._client
    assert coder_sdk.sent == ["hi"], coder_sdk.sent
    assistant_sdk = orch._drivers[assistant.id]._client
    assert assistant_sdk.sent == ["one"], assistant_sdk.sent  # rate-limited prompt never reached the SDK

    client.portal.call(orch.close_session, coder.id)
    client.portal.call(orch.close_session, assistant.id)
