"""Wave-3 cursor catch-up on heal/reconnect (audit W2.3) — server end-to-end.

When a client re-attaches with a CONTIGUOUS srcSeq cursor (``sinceSrcSeq``) and the daemon
replay ring still covers it, the edge resumes the live stream FROM the ring at that cursor:
it sends a lightweight ``resumeOk`` and SKIPS the full disk replay (no replayReset /
replayComplete). If the ring evicted the anchor — or no cursor is sent, or we are in
local/in-process mode — it falls back to the full replay exactly as before.

These drive the REAL remote edge↔daemon transport (``remote_edge``): a real DaemonClient
over a UDS to a real daemon ring, so the coverage probe (RemoteOrchestrator.ring_covers →
daemon app.state.ring_covers) and the ring-anchored resume are exercised for real. The
covered path also asserts the tail arrives exactly-once with srcSeq continuity.
"""
import asyncio
import json as _json
import time as _time
from unittest import mock

import pytest

# Reuse the real remote-edge rig (real daemon thread + real edge over a UDS) and its helpers.
from server.tests.test_remote_mode_edge import (  # noqa: F401
    remote_edge as remote_edge,
    _IDENT,
    _rec,
)
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — local-mode harness


# ---------------------------------------------------------------------------
# A daemon-side driver whose events() publishes N deltas to the ring, then blocks
# so the stream stays live and the ring stays populated for a second attach.
# ---------------------------------------------------------------------------

class _RingEvent:
    def __init__(self, i: int):
        self._i = i

    def to_wire(self) -> dict:
        # No sessionId/seq — ws._forward_live stamps those on the edge. The daemon
        # assigns the ring seq (== the srcSeq the client will observe).
        return {"type": "textDelta", "turnId": "t1", "text": f"d{self._i}"}


class _RingDriver:
    """Daemon-side driver: publishes ``n`` deltas via the pump ONCE, then holds the stream
    open so the ring retains them for a later catch-up attach.

    ``_emitted`` makes events() idempotent under a pump RESTART: a real live driver streams
    from one persistent source across re-attaches, but this fake's blocking generator can be
    GC-cancelled (no strong ref) and re-pumped by the second attach's ensure_driver — without
    the guard that would republish the same deltas as fresh ring seqs and corrupt the tail."""

    def __init__(self, n: int):
        self._n = n
        self._emitted = False

    def current_status(self) -> str:
        return "idle"

    def queued(self) -> list:
        return []

    def queue_wire(self) -> list:
        return []

    def open_stream(self):
        driver = self

        class _Stream:
            turn_active = False
            turn_started_at = None

            def events(self):
                return driver.events()

            def close(self):
                pass

        return _Stream()

    async def events(self):
        if not self._emitted:
            self._emitted = True
            for i in range(self._n):
                yield _RingEvent(i)
        await asyncio.Event().wait()  # block: keep the stream live, ring retained


def _flatten(msg: dict) -> list[dict]:
    return msg.get("events", []) if msg.get("type") == "batch" else [msg]


def _collect_until(ws, predicate, *, timeout: float = 6.0) -> list[dict]:
    """Drain flattened frames until predicate(list) is True or timeout."""
    events: list[dict] = []
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if predicate(events):
            return events
        try:
            events.extend(_flatten(ws.receive_json()))
        except Exception:
            break
    return events


def _types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def _count(events: list[dict], t: str) -> int:
    return sum(1 for e in events if e.get("type") == t)


@pytest.fixture()
def live_ring_edge(remote_edge):
    """remote_edge with orch.ensure_driver → a _RingDriver(4) and no on-disk transcript,
    plus socket A already attached and drained past the 4 streamed deltas (srcSeq 0..3),
    so the daemon ring holds seq 0..3 for a second (catch-up) attach."""
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    driver = _RingDriver(4)
    with mock.patch.object(orch, "ensure_driver", new=mock.AsyncMock(return_value=driver)), \
         mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=None)):
        with client.websocket_connect("/ws", headers=_IDENT) as wsA:
            wsA.send_json({"type": "attach", "sessionId": "s1"})
            evA = _collect_until(wsA, lambda acc: _count(acc, "textDelta") >= 4)
            deltas = [e for e in evA if e.get("type") == "textDelta"]
            assert [e["srcSeq"] for e in deltas] == [0, 1, 2, 3], evA
            yield orch, client


def test_covered_cursor_attach_gets_resumeOk_no_replay_tail_exactly_once(live_ring_edge):
    """Covered cursor → resumeOk, NO replayReset/replayComplete, and the ring tail
    (srcSeq cursor+1…) arrives exactly-once with srcSeq continuity."""
    orch, client = live_ring_edge

    # A separate socket re-attaches with a contiguous cursor at srcSeq 1 (the ring still
    # holds 0..3, so 2,3 are the missed tail).
    with client.websocket_connect("/ws", headers=_IDENT) as wsB:
        wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1})
        evB = _collect_until(
            wsB, lambda acc: any(e.get("type") == "resumeOk" for e in acc)
            and _count(acc, "textDelta") >= 2)

    assert _count(evB, "resumeOk") == 1, _types(evB)
    # Catch-up path: NEITHER full-replay bracket is sent.
    assert _count(evB, "replayReset") == 0, _types(evB)
    assert _count(evB, "replayComplete") == 0, _types(evB)
    # The missed tail: exactly seq 2,3 by source seq, exactly once each, in order.
    tail = [e for e in evB if e.get("type") == "textDelta"]
    assert [e["srcSeq"] for e in tail] == [2, 3], evB
    assert [e["text"] for e in tail] == ["d2", "d3"], evB


def test_ring_covers_rpc_round_trip(live_ring_edge):
    """RemoteOrchestrator.ring_covers over the real daemon RPC: covered within the window,
    not covered when the cursor is ahead of head (epoch guard)."""
    _orch, client = live_ring_edge
    sessions = client.app.state.sessions
    assert client.portal.call(sessions.ring_covers, "s1", 1) is True
    assert client.portal.call(sessions.ring_covers, "s1", 3) is True   # == head
    assert client.portal.call(sessions.ring_covers, "s1", 4) is False  # ahead of head
    assert client.portal.call(sessions.ring_covers, "s1", None) is False


def test_evicted_cursor_falls_back_to_full_replay(live_ring_edge):
    """When the ring cannot cover the cursor (ring_covers False), the attach falls back to
    the full disk replay: replayComplete is sent and NO resumeOk — the fallback is airtight
    (the not-covered decision is made before anything is enqueued)."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    with mock.patch.object(sessions, "ring_covers", new=mock.AsyncMock(return_value=False)):
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


def test_attach_without_cursor_never_probes_and_full_replays(live_ring_edge):
    """No cursor → full replay, and ring_covers is NEVER consulted (the probe is gated on a
    present cursor, so a plain attach costs no extra RPC)."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    with mock.patch.object(sessions, "ring_covers",
                           new=mock.AsyncMock(side_effect=AssertionError("must not probe"))) as spy:
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1"})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    spy.assert_not_awaited()
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


# ---------------------------------------------------------------------------
# Local / in-process mode ignores the cursor entirely (ring_covers is a constant
# False on the InProcessOrchestratorAdapter — there is no daemon ring to resume from).
# ---------------------------------------------------------------------------

def test_local_mode_ignores_cursor_and_full_replays(ws_env):
    from claude_agent_sdk import TextBlock
    from server.tests.fakes import fake_sdk as f
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-1", uuid="t1")]
    _app, orch, client = ws_env(msgs)
    session = client.portal.call(orch.create_session, "local-cursor")
    sid = session.id
    with client.websocket_connect("/ws", headers={"Tailscale-User-Login": "wstest@example.com"}) as ws:
        # Even WITH a cursor, local mode full-replays (ring_covers → False) — no resumeOk.
        ws.send_text(_json.dumps({"type": "attach", "sessionId": sid, "sinceSrcSeq": 3}))
        events = _collect_until(ws, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    assert _count(events, "replayComplete") == 1, _types(events)
    assert _count(events, "resumeOk") == 0, _types(events)
    client.portal.call(orch.close_session, sid)
