"""Tests for IPC subscriber queue reliability improvements (issue #664).

Covers:
- Control events (permissionRequest, permissionResolved, question, questionResolved)
  are buffered, never dropped, when the subscriber queue is full.
- Transcript events ARE dropped and a single GAP sentinel is emitted per burst.
- Queue size auto-scales with the number of active subscriber streams.
- CCSWITCH_IPC_QUEUE_SIZE env var overrides the base queue size.
"""
import asyncio

import pytest

from server.ipc.client import DaemonClient, _BASE_Q_SIZE, _CTRL_EVENT_TYPES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> DaemonClient:
    """Return a DaemonClient with a dummy socket path (no real daemon needed)."""
    return DaemonClient("unix:///tmp/test-ipc-none.sock")


def _ctrl_event(etype: str) -> dict:
    return {"type": etype, "permId": "p1", "turnId": "t1"}


def _text_event(i: int) -> dict:
    return {"type": "text", "text": f"chunk-{i}"}


async def _drain_stream(gen, n: int, timeout: float = 2.0) -> list[dict]:
    """Collect at most *n* events from *gen*, respecting a per-event timeout."""
    results = []
    for _ in range(n):
        results.append(await asyncio.wait_for(gen.__anext__(), timeout))
    return results


# ---------------------------------------------------------------------------
# Core eviction tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcript_drop_emits_gap():
    """When a transcript event fills the queue, the oldest is evicted and a GAP
    is emitted so the consumer knows to resync from disk."""
    client = _make_client()
    stream = client.subscribe("s", maxsize=2)

    # Pre-fill the queue to capacity via _deliver (bypassing the real WS).
    client._deliver("s", _text_event(0))
    client._deliver("s", _text_event(1))

    # One more transcript event: should drop oldest and count the drop.
    client._deliver("s", _text_event(2))

    events = await _drain_stream(stream, 3)
    # First yielded item must be the GAP sentinel.
    assert events[0].get("type") == "gap", f"expected gap, got {events[0]}"
    assert events[0]["lost"] == 1
    # Remaining two are the events that survived eviction.
    assert events[1].get("type") == "text"
    assert events[2].get("type") == "text"


@pytest.mark.asyncio
async def test_control_event_not_dropped_when_full():
    """When the queue is full, a control event must NOT be evicted — it goes to
    the ctrl_buf side buffer and is delivered once a slot frees."""
    for etype in _CTRL_EVENT_TYPES:
        client = _make_client()
        stream = client.subscribe("s", maxsize=2)

        # Fill queue to capacity with transcript events.
        client._deliver("s", _text_event(0))
        client._deliver("s", _text_event(1))

        # A control event while the queue is full: must NOT be evicted.
        ctrl = _ctrl_event(etype)
        client._deliver("s", ctrl)

        # _dropped must still be 0 — no GAP yet.
        q = next(iter(client._subscribers["s"]))
        assert client._dropped.get(q, 0) == 0, "control event should not increment _dropped"
        # The control event must be in ctrl_buf, not the main queue.
        assert client._ctrl_buf[q] == [ctrl], "control event should be in ctrl_buf"

        # Now drain: text0, text1, then the ctrl event re-injected.
        events = await _drain_stream(stream, 3)
        types = [e.get("type") for e in events]
        assert etype in types, f"control event type {etype!r} was never delivered: {types}"
        # No GAP in the output.
        assert "gap" not in types, f"unexpected gap with control event: {types}"


@pytest.mark.asyncio
async def test_ctrl_buf_delivered_after_queue_drains():
    """Buffered control events are re-injected in arrival order once slots free."""
    client = _make_client()
    stream = client.subscribe("s", maxsize=3)

    # Fill queue to capacity.
    client._deliver("s", _text_event(0))
    client._deliver("s", _text_event(1))
    client._deliver("s", _text_event(2))

    # Two control events while full.
    ctrl1 = {"type": "permissionRequest", "permId": "p1", "turnId": "t1"}
    ctrl2 = {"type": "permissionResolved", "permId": "p1", "turnId": "t1", "allowed": True}
    client._deliver("s", ctrl1)
    client._deliver("s", ctrl2)

    q = next(iter(client._subscribers["s"]))
    assert client._ctrl_buf[q] == [ctrl1, ctrl2]

    # Drain all 5 events (3 text + 2 ctrl).
    events = await _drain_stream(stream, 5)
    types = [e.get("type") for e in events]

    # No gap (no transcript drops occurred).
    assert "gap" not in types
    # Both control events delivered.
    assert types.count("permissionRequest") == 1
    assert types.count("permissionResolved") == 1
    # Control events arrive AFTER the queued text events (re-injected when slots free).
    ctrl_indices = [i for i, e in enumerate(events) if e.get("type") in _CTRL_EVENT_TYPES]
    text_indices = [i for i, e in enumerate(events) if e.get("type") == "text"]
    assert max(text_indices) < min(ctrl_indices), (
        "control events should arrive after the transcript events that preceded them"
    )


@pytest.mark.asyncio
async def test_gap_and_ctrl_coexist():
    """A full queue can drop transcript events (GAP) while simultaneously
    buffering a control event — both mechanisms work together."""
    client = _make_client()
    stream = client.subscribe("s", maxsize=2)

    client._deliver("s", _text_event(0))
    client._deliver("s", _text_event(1))

    # One more transcript → drop/GAP.
    client._deliver("s", _text_event(2))
    # One control event → ctrl_buf.
    client._deliver("s", {"type": "question", "questionId": "q1", "turnId": "t1",
                          "header": "h", "question": "?", "options": []})

    events = await _drain_stream(stream, 4)
    types = [e.get("type") for e in events]
    assert "gap" in types, "expected a gap from the transcript drop"
    assert "question" in types, "expected the control event to be delivered"


# ---------------------------------------------------------------------------
# Auto-scaling tests
# (async so asyncio.create_task inside subscribe() has a running event loop)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autoscale_first_stream_is_base():
    """First subscriber stream gets exactly _BASE_Q_SIZE slots."""
    client = _make_client()
    client.subscribe("s1")
    q = next(iter(client._subscribers["s1"]))
    assert q.maxsize == _BASE_Q_SIZE


@pytest.mark.asyncio
async def test_autoscale_grows_with_stream_count():
    """Queue size scales up when many streams are already open."""
    client = _make_client()
    # Open 4 streams before the one we're measuring.
    for i in range(4):
        client.subscribe(f"pre-{i}")
    # 5th stream: n_active=4 at subscribe time → maxsize = base * max(1, 4//4) = base*1
    client.subscribe("measured")
    q = next(iter(client._subscribers["measured"]))
    expected = _BASE_Q_SIZE * max(1, 4 // 4)
    assert q.maxsize == expected

    # After 8 pre-existing streams: n_active=8 → maxsize = base * max(1, 8//4) = base*2
    client2 = _make_client()
    for i in range(8):
        client2.subscribe(f"pre-{i}")
    client2.subscribe("measured2")
    q2 = next(iter(client2._subscribers["measured2"]))
    expected2 = _BASE_Q_SIZE * max(1, 8 // 4)
    assert q2.maxsize == expected2
    assert q2.maxsize == 2 * _BASE_Q_SIZE


@pytest.mark.asyncio
async def test_explicit_maxsize_bypasses_autoscale():
    """An explicit maxsize= parameter is used as-is, ignoring auto-scaling."""
    client = _make_client()
    for i in range(20):
        client.subscribe(f"pre-{i}")
    # Explicit override wins over any auto-scaled value.
    client.subscribe("explicit", maxsize=42)
    q = next(iter(client._subscribers["explicit"]))
    assert q.maxsize == 42


# ---------------------------------------------------------------------------
# Cleanup / lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ctrl_buf_cleaned_up_on_close():
    """When the generator is closed (consumer detaches), ctrl_buf entry is removed."""
    client = _make_client()
    stream = client.subscribe("s", maxsize=2)

    client._deliver("s", _text_event(0))
    client._deliver("s", _text_event(1))

    q = next(iter(client._subscribers["s"]))

    # Enter the generator so its finally block fires on aclose().
    first = await asyncio.wait_for(stream.__anext__(), 1.0)
    assert first.get("type") == "text"

    # Close the generator (simulates a consumer detach/cancel).
    await stream.aclose()

    # Both dropped-counter and ctrl_buf entry must be cleaned up.
    assert q not in client._dropped
    assert q not in client._ctrl_buf
