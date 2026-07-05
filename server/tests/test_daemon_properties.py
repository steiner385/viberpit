"""Property-based invariants for the session-daemon ring/publish/resume (audit wave 3).

The daemon's ordering / no-reuse / gap-signalling guarantees were only ever exercised
by hand-picked example sequences. These are exactly the invariants randomized inputs
break, so this module drives `server/ipc/daemon.py` with Hypothesis-generated event
streams and subscriber anchors and asserts the contract holds under arbitrary
interleavings.

Three properties:
  A. Published seqs are strictly monotonic per stream, contiguous from 0, never reused —
     even when many streams are interleaved (per-stream independence).
  B. A subscriber resuming at `since=s` receives EXACTLY the retained events with seq>s,
     in order — preceded by a single GAP control frame iff `s+1` predates the oldest
     retained event (an eviction hole), never a silent omission.
  C. `turn_open` / `resume_since` / `turn_started_at` obey the turn-boundary contract
     under any status/turnEnd/turnFailure ordering: the turn is open iff a `user` event
     is the most recent turn-boundary event with no closing event after it.

Runs are bounded (`@settings(max_examples=…, deadline=…)`) so the module stays CI-fast
(< ~30s): the two pure-state properties run many examples cheaply; property B spins a
real uvicorn+WS per example, so it runs far fewer.
"""
import asyncio
import json
import os

import pytest
import websockets
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from server.ipc import protocol as proto
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds

# The daemon's turn-close signals (mirrored from daemon.py as the CONTRACT under test).
_TURN_END_EVENTS = {"turnEnd", "turnFailure"}
_TERMINAL_STATUSES = {"done", "complete", "needs_attention"}
# `idle` and non-terminal states must NOT close a turn.
_STATUS_STATES = sorted(_TERMINAL_STATUSES | {"idle", "working", "error", "queued"})

_STREAM_NAMES = ["s0", "s1", "s2", "s3"]


# ── Event strategies ──────────────────────────────────────────────────────────

def _turn_events() -> st.SearchStrategy[dict]:
    """Events that drive turn-boundary state: user opens; turnEnd/turnFailure and a
    terminal `status` close; everything else is turn-neutral content."""
    return st.one_of(
        st.just({"type": "user", "text": "hi"}),
        st.just({"type": "textDelta", "text": "x"}),
        st.just({"type": "toolCall", "toolId": "t", "name": "Bash"}),
        st.just({"type": "turnEnd", "turnId": "t"}),
        st.just({"type": "turnFailure", "kind": "rate_limit"}),
        st.sampled_from(_STATUS_STATES).map(lambda s: {"type": "status", "state": s}),
    )


def _any_events() -> st.SearchStrategy[dict]:
    """Opaque payloads for pure ring/seq properties — the ring doesn't inspect content
    except for the turn-boundary types, which property C covers separately."""
    return st.builds(lambda i: {"type": "e", "i": i}, st.integers(min_value=0, max_value=99))


# ── Property A: seq monotonicity / no-reuse across interleaved streams ─────────

@settings(max_examples=250, deadline=None)
@given(
    ops=st.lists(
        st.tuples(st.sampled_from(_STREAM_NAMES), _any_events()),
        min_size=0, max_size=120,
    )
)
def test_published_seqs_strictly_monotonic_per_stream(ops):
    app = create_daemon_app()

    async def run():
        # Independent expected next-seq per stream; the daemon starts each at -1 and
        # assigns prev+1 on every publish.
        expected_next = {}
        seen = {}  # stream -> list of assigned seqs (to check uniqueness + order)
        for stream, event in ops:
            await app.state.publish(stream, event)
            want = expected_next.get(stream, -1) + 1
            got = app.state.head_seq(stream)
            assert got == want, f"stream {stream}: head_seq {got} != expected {want}"
            expected_next[stream] = want
            seen.setdefault(stream, []).append(got)

        # Per stream: seqs are 0..k contiguous, strictly increasing, no dup/reuse, and
        # the retained ring never holds a seq greater than head_seq.
        for stream, seqs in seen.items():
            assert seqs == list(range(len(seqs)))
            assert len(set(seqs)) == len(seqs)
            buf = app.state.buffers.get(stream)
            if buf:
                buf_seqs = [s for s, _ in buf]
                assert buf_seqs == sorted(buf_seqs)
                assert len(set(buf_seqs)) == len(buf_seqs)
                assert max(buf_seqs) == app.state.head_seq(stream)

    asyncio.run(run())


# ── Property C: turn_open / resume_since / turn_started_at contract ────────────

def _expected_turn_open(events: list[dict]) -> bool:
    """CONTRACT oracle (independent of the implementation's if/elif): the turn is open
    iff the most recent turn-boundary event is a `user` with no closing event after it."""
    open_now = False
    for ev in events:
        et = ev.get("type")
        if et == "user":
            open_now = True
        elif et in _TURN_END_EVENTS:
            open_now = False
        elif et == "status" and ev.get("state") in _TERMINAL_STATUSES:
            open_now = False
    return open_now


@settings(max_examples=250, deadline=None)
@given(events=st.lists(_turn_events(), min_size=0, max_size=60))
def test_turn_open_resume_invariants(events):
    app = create_daemon_app()
    stream = "turnstream"

    async def run():
        last_user_seq = None  # seq of the most recent `user` event
        for i, ev in enumerate(events):
            await app.state.publish(stream, ev)
            if ev.get("type") == "user":
                last_user_seq = i  # seqs are 0..N-1 for a single stream

            prefix = events[: i + 1]
            want_open = _expected_turn_open(prefix)
            assert app.state.turn_open_for(stream) is want_open

            # resume_since: while open, resume from just before the open turn's user
            # message (replay the whole in-progress turn); otherwise from head (from-now).
            head = app.state.head_seq(stream)
            if want_open:
                assert app.state.resume_since(stream) == last_user_seq - 1
                # turn_started_at is a live float only while the turn is open.
                assert isinstance(app.state.turn_started_at(stream), float)
            else:
                assert app.state.resume_since(stream) == head
                assert app.state.turn_started_at(stream) is None

    asyncio.run(run())


# ── Property B: subscribe replay is exact-or-GAP, never silently omitting ──────

async def _drive_replay(events: list[dict], maxlen: int, since):
    """Publish `events` to a real daemon (ring capped at `maxlen`), then subscribe a real
    WS at `since` and return the frames received (bounded)."""
    prev = os.environ.get("CCSWITCH_IPC_REPLAY_BUFFER")
    os.environ["CCSWITCH_IPC_REPLAY_BUFFER"] = str(maxlen)
    try:
        app = create_daemon_app()
        stream = "s1"
        async with serve_daemon_uds(app) as sock:
            for ev in events:
                await app.state.publish(stream, ev)

            n = len(events)
            retained_seqs = list(range(n))[-maxlen:] if maxlen < n else list(range(n))
            oldest = retained_seqs[0] if retained_seqs else None
            delivered = [s for s in retained_seqs if since is None or s > since]
            gap = (
                since is not None and since >= 0 and oldest is not None
                and since + 1 < oldest
            )
            expected_total = len(delivered) + (1 if gap else 0)

            async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
                await ws.send(json.dumps(proto.subscribe_frame(stream, since=since)))
                frames = []
                if expected_total == 0:
                    # Nothing should be replayed — assert the socket stays silent.
                    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                        await asyncio.wait_for(ws.recv(), 0.25)
                else:
                    for _ in range(expected_total):
                        frames.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
            return frames, delivered, gap, oldest, since
    finally:
        if prev is None:
            os.environ.pop("CCSWITCH_IPC_REPLAY_BUFFER", None)
        else:
            os.environ["CCSWITCH_IPC_REPLAY_BUFFER"] = prev


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    events=st.lists(_any_events(), min_size=1, max_size=12),
    maxlen=st.integers(min_value=1, max_value=10),
    since=st.one_of(st.none(), st.integers(min_value=-1, max_value=14)),
)
def test_subscribe_replay_exact_or_gap(events, maxlen, since):
    frames, delivered, gap, oldest, since_v = asyncio.run(
        _drive_replay(events, maxlen, since)
    )

    idx = 0
    if gap:
        g = frames[0]
        assert g["type"] == proto.GAP
        assert g["stream"] == "s1"
        assert g["oldest"] == oldest
        assert g["lost"] == oldest - (since_v + 1)
        idx = 1

    body = frames[idx:]
    # Exactly the retained events with seq>since, in strictly increasing seq order.
    got_seqs = [f["seq"] for f in body]
    assert got_seqs == delivered
    assert all(f["type"] == proto.EVENT for f in body)
    assert got_seqs == sorted(got_seqs)
    assert len(set(got_seqs)) == len(got_seqs)
    # And nothing below the resume anchor leaked through.
    if since_v is not None:
        assert all(s > since_v for s in got_seqs)
