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


@pytest.mark.asyncio
async def test_command_result_is_consumed_and_sent_top_level():
    """D-16a: an app command typed as a prompt runs in the orchestrator's send_prompt,
    whose result rides the live stream as ``commandResult`` — sent as the top-level
    frame the client reads, never seq'd into the transcript batch."""
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()
    sent: list = []

    async def on_result(sid, wire):
        sent.append((sid, wire))

    stream = _Stream([_Ev({"type": "textDelta", "text": "a"}),
                      _Ev({"type": "commandResult", "ok": True, "name": "fast",
                           "render": "notice", "data": {"text": "x"}}),
                      _Ev({"type": "textDelta", "text": "b"})])
    await _forward_live(stream, out_q, "s1", on_command_result=on_result)
    forwarded = [out_q.get_nowait() for _ in range(out_q.qsize())]
    assert [f["seq"] for f in forwarded] == [0, 1]
    assert sent == [("s1", {"type": "commandResult", "ok": True, "name": "fast",
                            "render": "notice", "data": {"text": "x"}})]


@pytest.mark.parametrize("text,attachments", [
    ("/fast on", None), ("/effort high", None), ("/output-style Concise", None),
    ("/output-style Concise", ["att-1"]),      # attachments no longer bypass the check
])
def test_a_typed_app_command_never_reaches_the_cli_and_answers_with_a_notice(
        ws_env, text, attachments):
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "app-only").id
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        driver = orch._drivers[sid]
        handed: list[str] = []
        real = driver.send_prompt

        async def spy(t, *a, **k):
            handed.append(t)
            return await real(t, *a, **k)

        driver.send_prompt = spy
        frame = {"type": "prompt", "sessionId": sid, "text": text}
        if attachments:
            frame["attachmentIds"] = attachments
        ws.send_text(json.dumps(frame))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "commandResult")
    result = next(f for f in frames if f.get("type") == "commandResult")
    assert result["render"] == "notice" and result["sessionId"] == sid
    assert handed == []


@pytest.mark.asyncio
async def test_consumed_commands_changed_stamps_src_seq_skip_on_the_next_frame():
    """#3278: the client's srcSeq tracker never sees the consumed seq; the next frame says so."""
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()

    class _Src(_Ev):
        def __init__(self, wire, src): super().__init__(wire); self.src_seq = src

    stream = _Stream([_Src({"type": "textDelta"}, 5), _Src({"type": "commandsChanged"}, 6),
                      _Src({"type": "textDelta"}, 7), _Src({"type": "textDelta"}, 8)])
    await _forward_live(stream, out_q, "s1")
    frames = [out_q.get_nowait() for _ in range(out_q.qsize())]
    assert [f.get("srcSeqSkip") for f in frames] == [None, 1, None]


@pytest.mark.asyncio
async def test_consumed_command_result_stamps_src_seq_skip_too():
    """D-16a + #3283: a consumed ``commandResult`` hides a source seq exactly like
    ``commandsChanged``, so back-to-back consumed frames add up on the next one."""
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()

    class _Src(_Ev):
        def __init__(self, wire, src): super().__init__(wire); self.src_seq = src

    stream = _Stream([_Src({"type": "textDelta"}, 5),
                      _Src({"type": "commandResult", "ok": True, "name": "fast"}, 6),
                      _Src({"type": "commandsChanged"}, 7), _Src({"type": "textDelta"}, 8)])
    await _forward_live(stream, out_q, "s1")
    frames = [out_q.get_nowait() for _ in range(out_q.qsize())]
    assert [f.get("srcSeqSkip") for f in frames] == [None, 2]


def _send_typed(ws_env, text, *, headers=_IDENTITY_HEADERS, attach_first=None, patches=()):
    """Attach (optionally as another user first), type *text*, collect frames until the
    promptAck. Returns (frames, prompts handed to the CLI driver, orch, sid)."""
    import contextlib
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "app-only").id
    handed: list[str] = []
    if attach_first is not None:
        with client.websocket_connect("/ws", headers=attach_first) as ws0:
            ws0.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            _drain_events(ws0, until_type="replayComplete")
    with contextlib.ExitStack() as stack, \
            client.websocket_connect("/ws", headers=headers) as ws:
        for p in patches:
            stack.enter_context(p)
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        driver = orch._drivers[sid]
        real = driver.send_prompt

        async def spy(t, *a, **k):
            handed.append(t)
            return await real(t, *a, **k)

        driver.send_prompt = spy
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": text}))
        # The ack and the commandResult (it rides the live stream) arrive in either
        # order: drain until both are in.
        frames: list = []

        def _final_ack(f) -> bool:
            return f.get("type") == "promptAck" and f.get("stage") in ("delivered", "failed")

        for _ in range(4):
            if (any(_final_ack(f) for f in frames)
                    and any(f.get("type") == "commandResult" for f in frames)):
                break
            frames += _drain_events(ws, until=lambda f: _final_ack(f)
                                    or f.get("type") == "commandResult")
    _send_typed.client = client                                 # type: ignore[attr-defined]
    return frames, handed, orch, sid


def test_a_typed_command_is_audited_as_the_sender_not_the_first_attacher(ws_env):
    """S2: the driver identity is pinned to whoever attached first; the audit row must
    name the person who typed the command."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    frames, handed, orch, sid = _send_typed(
        ws_env, "/plan on", headers={"Tailscale-User-Login": "bob@example.com"},
        attach_first={"Tailscale-User-Login": "alice@example.com"})

    async def rows():
        async with db_session() as db:
            return [(r.identity, r.detail.get("mode")) for r in (await db.execute(
                select(AuditEvent).where(AuditEvent.type == "permission_mode.change"))
                ).scalars().all()]

    assert handed == []
    assert [f for f in frames if f.get("type") == "promptAck"][-1]["stage"] == "delivered"
    assert orch._drivers[sid].identity == "alice@example.com"    # pinned to the first
    assert _send_typed.client.portal.call(rows) == [("bob@example.com", "plan")]  # type: ignore[attr-defined]


def test_a_typed_command_with_more_text_is_refused_and_not_delivered(ws_env):
    """M1: nothing runs, nothing is sent, and the ack is terminal "failed" — the bubble
    keeps the text and the browser never auto-resends it."""
    frames, handed, orch, sid = _send_typed(ws_env, "/plan\nnow write the migration")
    assert handed == [] and orch._drivers[sid].mode != "plan"
    acks = [f for f in frames if f.get("type") == "promptAck"]
    assert acks[-1]["stage"] == "failed"
    assert not any(f.get("type") == "promptRejected" for f in frames)
    notice = next(f for f in frames if f.get("type") == "commandResult")
    assert "the text after it" in notice["data"]["text"]


def test_a_typed_command_that_raises_answers_ok_false(ws_env):
    """M2: a failing command is an ok:false commandResult plus a delivered ack — never
    the receive loop's catch-all."""
    from unittest.mock import patch

    from server.sessions.orchestrator import SessionOrchestrator
    frames, handed, _orch, _sid = _send_typed(
        ws_env, "/plan on", patches=[patch.object(
            SessionOrchestrator, "set_permission_mode", side_effect=RuntimeError("boom"))])
    result = next(f for f in frames if f.get("type") == "commandResult")
    assert result["ok"] is False and result["error"] == "command failed"
    assert [f for f in frames if f.get("type") == "promptAck"][-1]["stage"] == "delivered"
    assert handed == []
