"""Multi-client WS concurrency seam tests — audit wave 3 (test-infra gaps #2/#3/#4).

The WS edge (server/bridge/ws.py) fans one live driver stream out to N sockets via
``driver.open_stream()`` (each attach gets its own snapshot + subscriber queue). The
existing test_ws.py "second client" cases are all SEQUENTIAL (attach, detach, reattach);
NONE exercise two sockets open on one session AT THE SAME TIME, none drive live events
WHILE a replay batch streams to another client, and none drop a socket mid-streaming-turn.
Those are exactly the #422/#458-class regressions this file covers.

Harness
-------
This module adds a small multi-client harness on top of test_ws.py's ``ws_env``:
  * ``open_attached(client, session_id, n)`` — open N websocket_connect clients
    concurrently against one session and attach each (drained past replayComplete);
  * ``collect(ws, predicate, ...)`` — collect a client's ORDERED, batch-flattened frame
    list until a predicate over the accumulated list is satisfied (a bounded wall-clock
    timeout is a failsafe only — the assertions key off deterministic markers/gates).

Determinism
-----------
No sleep-based races. Live turns are gated with ``_GatedSDK`` (an asyncio.Event the
turn blocks on right before its ResultMessage), so a test can pin "the turn is mid-stream"
to a real signal (the pre-result deltas have arrived; turnEnd has NOT) and only then join /
drop a second client, then release the gate.

``_GatedSDK`` duplicates the local-gate idea of the ``_GatedSDK`` pattern the brief points
at (test_driver_replay_buffer.py); that file does not exist on this base, so the pattern is
inlined here. FOLLOW-UP: if a shared gating generator lands in server/tests/fakes/fake_sdk.py,
route this through it and delete this local copy (constitution VI — single source of truth).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable

import pytest
from claude_agent_sdk import ResultMessage, TextBlock

from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient
# Reuse the real WS test rig: the fixture, the identity headers, and the
# batch-flattening drain helper — no parallel copy of any of them (constitution VI).
# ``ws_env as ws_env`` is an explicit re-export: it makes the imported pytest fixture
# available for parameter injection in this module while telling ruff the binding is
# intentional (so a test's ``ws_env`` parameter isn't flagged F811 against it).
from server.tests.test_ws import (  # noqa: F401
    ws_env as ws_env,
    _drain_events,
    _IDENTITY_HEADERS,
)


# ---------------------------------------------------------------------------
# Frame helpers — normalize away per-attach seq, isolate transcript content
# ---------------------------------------------------------------------------

# Transcript-reconstructing event types (the client's session reducer consumes these).
# Lifecycle/ephemeral frames (status, title, queueUpdate, commands, contextUsage,
# suggestions) are re-emitted from authoritative state per-attach and legitimately
# interleave differently between two sockets, so they are NOT part of the
# "same transcript in the same order" contract.
_TRANSCRIPT_TYPES = {
    "user", "turnStart", "turnModel", "sessionMeta",
    "textDelta", "thinkingDelta", "toolCall", "toolResult", "turnEnd",
}


def _strip_seq(e: dict) -> dict:
    """Drop the per-attach transport fields. ``seq`` restarts at 0 on every attach and
    ``srcSeq`` is telemetry only — neither is part of the event's identity, so two
    sockets' streams must match once they're removed."""
    return {k: v for k, v in e.items() if k not in ("seq", "srcSeq")}


def _transcript_frames(frames: list[dict]) -> list[dict]:
    """The seq-stripped subsequence of transcript-reconstructing events."""
    return [_strip_seq(e) for e in frames if e.get("type") in _TRANSCRIPT_TYPES]


def _reconstruct_text(frames: list[dict]) -> list[tuple[str, str]]:
    """Replay frames through a faithful model of the client's session reducer and
    return the accumulated per-turn text as ``[(turnId, text), ...]`` in first-seen order.

    Mirrors two client behaviours that make the multi-client contract checkable:
      * ``replayReset`` clears ALL accumulated transcript state (the client wipes and
        rebuilds atomically on every (re)attach), so a socket that re-attaches mid-turn
        ends on ONLY its post-reset content — no duplication from its earlier connection;
      * consecutive ``textDelta`` frames for a turn are APPENDED, so a late joiner's single
        coalesced snapshot delta reconstructs to the same string as many raw live deltas.
    """
    order: list[str] = []
    text: dict[str, str] = {}
    for e in frames:
        t = e.get("type")
        if t == "replayReset":
            order.clear()
            text.clear()
        elif t == "textDelta":
            tid = e.get("turnId", "")
            if tid not in text:
                text[tid] = ""
                order.append(tid)
            text[tid] += e.get("text", "")
    return [(tid, text[tid]) for tid in order]


def collect(ws, predicate: Callable[[list[dict]], bool], *, timeout: float = 8.0) -> list[dict]:
    """Collect *ws*'s batch-flattened frames until ``predicate(accumulated_list)`` is True.

    Unlike ``_drain_events`` (which matches a single event) this evaluates the WHOLE
    accumulated list, so a test can wait for "2 textDeltas have arrived" or "turnEnd is
    present". The timeout is a failsafe: every caller waits on a signal that WILL arrive
    (a gate the test itself releases, or content the turn definitely produces)."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(events):
            return events
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        if msg.get("type") == "batch":
            events.extend(msg.get("events", []))
        else:
            events.append(msg)
    return events


def _count_type(frames: list[dict], type_: str) -> int:
    return sum(1 for e in frames if e.get("type") == type_)


def _has_type(type_: str) -> Callable[[list[dict]], bool]:
    return lambda acc: any(e.get("type") == type_ for e in acc)


@contextlib.contextmanager
def open_attached(client, session_id: str, n: int):
    """Open *n* websocket_connect clients concurrently against one session and attach
    each, draining each past its replayComplete. Yields the list of live sockets; closes
    them all on exit. This is the multi-client fixture the sequential test_ws.py lacks."""
    with contextlib.ExitStack() as stack:
        sockets = []
        for _ in range(n):
            ws = stack.enter_context(
                client.websocket_connect("/ws", headers=_IDENTITY_HEADERS))
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            sockets.append(ws)
        yield sockets


# ---------------------------------------------------------------------------
# Gated fake SDK — blocks a turn right before its ResultMessage until released
# ---------------------------------------------------------------------------

class _GatedSDK(FakeSDKClient):
    """A fake SDK whose turn streams every message up to (but not including) its
    ``ResultMessage``, then blocks on ``self.gate`` until the test releases it. This pins
    the "turn is actively mid-stream" instant deterministically — no sleeps.

    The gate is created in ``__init__`` (run by the orchestrator on the app loop when the
    driver builds its client) and instances are registered in ``instances`` so the test can
    reach the live one and release it through ``client.portal``."""

    instances: list["_GatedSDK"] = []

    def __init__(self, messages, on_can_use_tool=None, **kw):
        super().__init__(messages, on_can_use_tool=on_can_use_tool, **kw)
        self.gate = asyncio.Event()
        _GatedSDK.instances.append(self)

    async def receive_response(self):
        for m in self._messages:
            if self.interrupted:
                break
            if isinstance(m, ResultMessage):
                # Turn is fully streamed but NOT settled — hold here until released.
                await self.gate.wait()
            yield m


@pytest.fixture()
def gated():
    """Reset the gated-SDK instance registry and hand back the class."""
    _GatedSDK.instances.clear()
    yield _GatedSDK
    _GatedSDK.instances.clear()


def _release_gate(client) -> None:
    """Release the most-recent gated turn on the app loop (portal-thread safe)."""
    client.portal.call(lambda: _GatedSDK.instances[-1].gate.set())


# Two raw text deltas → one logical assistant message "Hello world".
_TURN_MSGS = [
    f.sys_init("sess-1"),
    f.stream_start(uuid="t1"),
    f.stream_text("Hello ", uuid="t1"),
    f.stream_text("world", uuid="t1"),
    f.assistant(TextBlock(text="Hello world"), uuid="t1"),
    f.result(session_id="sess-1", uuid="t1"),
]


# ===========================================================================
# Seam 1 — two concurrently-live clients see the SAME transcript, SAME order
# ===========================================================================

def test_two_live_clients_receive_identical_transcript(ws_env):
    """Two sockets attached to one session BEFORE a turn starts must both receive the
    same transcript events in the same order once the turn completes (ignoring each
    attach's private ``seq``). The single-consumer regression this guards: a WORK queue
    would split events between the two, so neither would see the whole turn."""
    _app, orch, client = ws_env(_TURN_MSGS)
    session = client.portal.call(orch.create_session, "seam-identical")
    sid = session.id

    with open_attached(client, sid, 2) as (wsA, wsB):
        wsA.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hi"}))
        evA = collect(wsA, _has_type("turnEnd"))
        evB = collect(wsB, _has_type("turnEnd"))

    tA = _transcript_frames(evA)
    tB = _transcript_frames(evB)
    assert tA == tB, f"transcript streams diverged:\nA={tA}\nB={tB}"
    # And both actually saw the whole turn (not an empty match).
    assert _reconstruct_text(evA) == [("t1", "Hello world")], _reconstruct_text(evA)
    assert _reconstruct_text(evB) == [("t1", "Hello world")]
    client.portal.call(orch.close_session, sid)


# ===========================================================================
# Seam 2 — client B attaches (full replay) WHILE a turn streams to client A
# ===========================================================================

def test_client_b_attaches_mid_stream_no_dup_no_missing(ws_env, gated):
    """B attaches while A's turn is actively streaming (gated before its result). B's
    view = replayReset + replayComplete + the driver's atomic in-progress-turn snapshot
    (one coalesced delta) + the live tail (result→turnEnd). Once both settle, B's
    reconstructed turn text must equal A's — no duplicated and no missing turn content."""
    _app, orch, client = ws_env(_TURN_MSGS, client_cls=gated)
    session = client.portal.call(orch.create_session, "seam-midstream")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as wsA:
        wsA.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(wsA, until_type="replayComplete")
        wsA.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hi"}))
        # Deterministic mid-stream point: both deltas delivered to A, turn gated (no turnEnd).
        preA = collect(wsA, lambda acc: _count_type(acc, "textDelta") >= 2)
        assert not _has_type("turnEnd")(preA), "turn settled before B could join"

        # B joins mid-turn — full replay path runs concurrently with the live turn.
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as wsB:
            wsB.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            # B must observe the in-progress turn (via the open_stream snapshot) even
            # before release — assert the seam delivered it, not just eventual settle.
            preB = collect(wsB, lambda acc: _count_type(acc, "textDelta") >= 1)
            assert _reconstruct_text(preB) == [("t1", "Hello world")], (
                f"B's mid-stream snapshot lost/dup'd turn content: {_reconstruct_text(preB)}")

            _release_gate(client)
            tailA = collect(wsA, _has_type("turnEnd"))
            tailB = collect(wsB, _has_type("turnEnd"))

    fullA = preA + tailA
    fullB = preB + tailB
    assert _reconstruct_text(fullA) == [("t1", "Hello world")], _reconstruct_text(fullA)
    assert _reconstruct_text(fullB) == [("t1", "Hello world")], _reconstruct_text(fullB)
    # No duplicated turn content on B: exactly one turn, one settle.
    assert _count_type(fullB, "turnEnd") == 1, "B saw the turn end more than once (dup)"
    client.portal.call(orch.close_session, sid)


# ===========================================================================
# Seam 3 — client A drops mid-turn, reconnects; A's final view heals to B's
# ===========================================================================

def test_client_a_drops_and_reattaches_heals_to_b(ws_env, gated):
    """A and B both attach before the turn. The turn streams to both, then gates before
    its result. A's socket DROPS mid-turn; A reconnects and re-attaches (still mid-turn),
    receiving a replayReset + the in-progress-turn snapshot. After release+settle, A's
    reconstructed transcript (which its replayReset healed to only the post-reset content)
    equals B's — the dedup/heal path end-to-end."""
    _app, orch, client = ws_env(_TURN_MSGS, client_cls=gated)
    session = client.portal.call(orch.create_session, "seam-drop-heal")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as wsB:
        wsB.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(wsB, until_type="replayComplete")

        framesA: list[dict] = []
        framesB: list[dict] = []
        # --- A's first connection: attach, prompt, stream mid-turn, then DROP. ---
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as wsA1:
            wsA1.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            _drain_events(wsA1, until_type="replayComplete")
            wsA1.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hi"}))
            pre1 = collect(wsA1, lambda acc: _count_type(acc, "textDelta") >= 2)
            assert not _has_type("turnEnd")(pre1), "turn settled before the drop"
            framesA += pre1
            # B is watching the same live turn — keep its frames (it never re-attaches).
            framesB += collect(wsB, lambda acc: _count_type(acc, "textDelta") >= 2)
        # wsA1 closed here — A dropped mid-streaming-turn.

        # --- A's second connection: re-attach mid-turn, then release + settle. ---
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as wsA2:
            wsA2.send_text(json.dumps({"type": "attach", "sessionId": sid}))
            framesA += collect(wsA2, _has_type("replayComplete"))
            _release_gate(client)
            framesA += collect(wsA2, _has_type("turnEnd"))
            framesB += collect(wsB, _has_type("turnEnd"))

    # A's reducer wiped on the reconnect's replayReset, so its final view == B's.
    assert _reconstruct_text(framesA) == [("t1", "Hello world")], _reconstruct_text(framesA)
    assert _reconstruct_text(framesB) == [("t1", "Hello world")], _reconstruct_text(framesB)
    assert _reconstruct_text(framesA) == _reconstruct_text(framesB)
    client.portal.call(orch.close_session, sid)


# ===========================================================================
# Seam 4 — slow client does not stall the fast one; slow client catches up FULLY
# ===========================================================================

def test_slow_client_does_not_stall_fast_and_catches_up(ws_env):
    """Stall one client's reads while the other streams a burst. Documented out_q
    semantics in LOCAL/in-process mode: ``driver.open_stream()`` gives each subscriber its
    OWN unbounded ``asyncio.Queue`` written with ``put_nowait`` (server/sessions/driver.py
    ``_emit``), so a non-reading socket never applies backpressure to the driver and never
    drops events. Therefore the fast client is unaffected AND the slow client catches up
    COMPLETELY once it reads — there is NO gap/heal signal in this mode (the GAP sentinel
    only exists on the daemon ring-buffer / remote path). We assert exactly that branch."""
    burst = [f.sys_init("sess-1"), f.stream_start(uuid="t1")]
    burst += [f.stream_text(f"d{i} ", uuid="t1") for i in range(8)]
    burst += [f.assistant(TextBlock(text="".join(f"d{i} " for i in range(8))), uuid="t1"),
              f.result(session_id="sess-1", uuid="t1")]
    expected = "".join(f"d{i} " for i in range(8))

    _app, orch, client = ws_env(burst)
    session = client.portal.call(orch.create_session, "seam-slow")
    sid = session.id

    with open_attached(client, sid, 2) as (wsFast, wsSlow):
        # wsSlow is deliberately NOT read during the burst.
        wsFast.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "go"}))
        evFast = collect(wsFast, _has_type("turnEnd"))
        # Fast client completed the whole turn without the slow reader participating.
        assert _reconstruct_text(evFast) == [("t1", expected)], _reconstruct_text(evFast)

        # Now the slow client reads: it must catch up COMPLETELY — every delta, no gap.
        evSlow = collect(wsSlow, _has_type("turnEnd"))

    assert _reconstruct_text(evSlow) == [("t1", expected)], (
        f"slow client did not catch up completely: {_reconstruct_text(evSlow)}")
    # And it caught up via full delivery, not a gap/heal reset (no replayReset mid-turn).
    assert _count_type(evSlow, "replayReset") == 0, (
        "slow client received a gap/heal reset — unexpected in local unbounded-queue mode")
    assert _count_type(evSlow, "turnEnd") == 1
    client.portal.call(orch.close_session, sid)
