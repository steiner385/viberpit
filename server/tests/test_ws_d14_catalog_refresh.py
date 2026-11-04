"""D-14 final review: the ws bridge refreshes the command catalog when the session
says it changed, and an APP_ONLY name typed as a prompt never reaches the CLI."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws


class _Ev:
    def __init__(self, wire): self._wire = wire
    def to_wire(self): return dict(self._wire)


class _Stream:
    def __init__(self, events): self._events = events
    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_commands_changed_is_consumed_and_triggers_the_catalog_push():
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()
    pushed: list[str] = []

    async def push(sid: str) -> None:
        pushed.append(sid)

    stream = _Stream([_Ev({"type": "textDelta", "text": "a"}), _Ev({"type": "commandsChanged"}),
                      _Ev({"type": "textDelta", "text": "b"})])
    await _forward_live(stream, out_q, "s1", on_commands_changed=push)
    forwarded = [out_q.get_nowait() for _ in range(out_q.qsize())]
    assert [f["type"] for f in forwarded] == ["textDelta", "textDelta"]
    assert [f["seq"] for f in forwarded] == [0, 1]        # no hole: not a lost event
    assert pushed == ["s1"]


def test_a_typed_slash_fast_never_reaches_the_cli_and_answers_with_a_notice(ws_env):
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "app-only").id
    sent: list[str] = []
    real = orch.send_prompt

    async def spy(session_id, text, *a, **k):
        sent.append(text)
        return await real(session_id, text, *a, **k)

    orch.send_prompt = spy
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "/fast on"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "commandResult")
    result = next(f for f in frames if f.get("type") == "commandResult")
    assert result["render"] == "notice" and "isn't available" in result["data"]["text"]
    assert sent == []
