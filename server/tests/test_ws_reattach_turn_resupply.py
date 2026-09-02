"""Mid-turn re-attach must re-supply the open turn from the daemon ring.

Incident 2026-07-07 01:28Z (session fced5538): reloading a live autonomous session's
view mid-turn reset the transcript to a bare "Beginning of session" holding only the
events published AFTER the re-attach. The edge's disk replay had (correctly) dropped
the whole in-progress turn (``drop_incomplete_turn``), trusting the daemon ring to
re-supply it via the ``resume_since`` anchor — but the shared DaemonClient keeps ONE
daemon-side subscription per stream (server/ipc/client.py): only the FIRST local
subscriber ever triggers the daemon's replay-on-subscribe, and nothing removes a
stream from ``_desired``. Every later attach joined the live fan-out at "now", its
``since`` silently ignored, so the promised ring re-supply never arrived and the open
turn vanished from the view until it settled to disk. (#526 fixed the ring-EVICTION
variant of this symptom; this is the shared-fan-out variant — the ring still held
every event, seq 0 included.)

The fix: the attach path pins a one-shot ``ring_tail`` snapshot of the open turn
(``RemoteDriver.prepare_open_turn_resupply``) — the same atomic snapshot-then-dedup
machinery as the wave-3 cursor catch-up — instead of relying on the ignored
subscribe ``since``. If the snapshot is NOT covered (ring evicted in the window),
the attach degrades to the #526 safe shape: the open turn stays in the disk replay.

These drive the REAL remote edge↔daemon transport (``remote_edge``): a real
DaemonClient over a UDS to a real daemon ring.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

# Reuse the real remote-edge rig (real daemon thread + real edge over a UDS).
from server.tests.test_remote_mode_edge import (  # noqa: F401
    remote_edge as remote_edge,
    _IDENT,
    _rec,
)
from server.tests.test_ws_cursor_catchup import _collect_until, _count, _types


class _TurnWire:
    def __init__(self, wire: dict):
        self._wire = wire

    def to_wire(self) -> dict:
        return self._wire


class _OpenTurnRingDriver:
    """Daemon-side driver: publishes ONE user event (opening the daemon's turn
    tracking) then ``n`` deltas — the turn stays OPEN and the ring retains the
    whole turn (seq 0..n) for a later re-attach. It then emits slow ``hb``
    heartbeat deltas forever: the TestClient's ``receive_json`` has no timeout,
    so a steady trickle keeps ``_collect_until``'s deadline check reachable — a
    broken attach then FAILS its assertions instead of hanging the receive.
    ``_emitted`` guards a pump restart from republishing (same idempotence shape
    as _RingDriver)."""

    def __init__(self, n: int):
        self._n = n
        self._emitted = False

    # Registered in orch._drivers (detached derives from that), so the live-attach
    # extras consult it like any live driver: nothing pending, no catalog, no seeding.
    def pending_questions(self) -> list:
        return []

    def pending_permissions(self) -> list:
        return []

    async def server_info(self):
        return None

    def seed_idle_suggestions(self, *a, **k) -> None:
        return None

    def current_status(self) -> str:
        return "in_progress"

    def queued(self) -> list:
        return []

    def queue_wire(self) -> list:
        return []

    def open_stream(self):
        driver = self

        class _Stream:
            turn_active = True
            turn_started_at = None

            def events(self):
                return driver.events()

            def close(self):
                pass

        return _Stream()

    async def events(self):
        if not self._emitted:
            self._emitted = True
            yield _TurnWire({"type": "user", "turnId": "t1", "text": "go"})
            for i in range(self._n):
                yield _TurnWire({"type": "textDelta", "turnId": "t1", "text": f"d{i}"})
        while True:  # keep the stream live; the turn never settles
            await asyncio.sleep(0.25)
            yield _TurnWire({"type": "textDelta", "turnId": "t1", "text": "hb"})


@pytest.fixture()
def open_turn_edge(remote_edge):
    """remote_edge whose driver opened a turn (user + 4 deltas, seq 0..4, all retained)
    that never settles, with socket A attached, drained, and CLOSED — the navigated-away
    viewer of the incident. The shared DaemonClient now already "desires" the stream, so
    a naive re-attach would join the fan-out at "now" and miss the whole open turn."""
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    driver = _OpenTurnRingDriver(4)
    # The real ensure_driver registers the driver it returns; `detached` is DERIVED from
    # that registration (no driver → read-only replay attach), so the mock must mirror it.
    orch._drivers["s1"] = driver
    with mock.patch.object(orch, "ensure_driver", new=mock.AsyncMock(return_value=driver)), \
         mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=None)):
        with client.websocket_connect("/ws", headers=_IDENT) as wsA:
            wsA.send_json({"type": "attach", "sessionId": "s1"})
            evA = _collect_until(wsA, lambda acc: _saw_turn(acc, 4))
            assert _saw_turn(evA, 4), _types(evA)
        yield orch, client


def _saw_turn(acc: list[dict], n: int) -> bool:
    """True once the open turn (srcSeq 0..n — the user event + its n deltas) has
    been delivered; heartbeat deltas (srcSeq > n) never satisfy it."""
    seen = {e.get("srcSeq") for e in acc}
    return all(s in seen for s in range(n + 1))


def test_reattach_mid_turn_resupplies_open_turn_from_ring(open_turn_edge):
    """The incident regression: a full-replay re-attach while the turn is open (no
    cursor — a reload / nav-return) must deliver the open turn's ring events (user +
    deltas, srcSeq 0..4) exactly once after the replay bracket — NOT an empty view
    that only fills with events published after the attach."""
    orch, client = open_turn_edge
    with client.websocket_connect("/ws", headers=_IDENT) as wsB:
        wsB.send_json({"type": "attach", "sessionId": "s1"})
        evB = _collect_until(wsB, lambda acc: _saw_turn(acc, 4))

    # Full-replay bracket (this is not the cursor catch-up: no resumeOk).
    assert _count(evB, "resumeOk") == 0, _types(evB)
    assert _count(evB, "replayReset") == 1, _types(evB)
    assert _count(evB, "replayComplete") == 1, _types(evB)
    # The open turn arrives from the ring snapshot: exactly once, in order, seq 0..4
    # (heartbeat deltas published after the attach carry srcSeq > 4 and are ignored).
    turn = [e for e in evB if e.get("srcSeq") in (0, 1, 2, 3, 4)]
    assert [e.get("srcSeq") for e in turn] == [0, 1, 2, 3, 4], evB
    assert [e.get("type") for e in turn] == ["user"] + ["textDelta"] * 4, evB
    assert [e.get("text") for e in turn][1:] == ["d0", "d1", "d2", "d3"], evB


def test_resupply_not_covered_degrades_to_undropped_disk_replay(open_turn_edge, tmp_path):
    """Eviction race (the #526 degraded shape at attach granularity): when the ring
    snapshot comes back NOT covered, the attach must keep the open turn in the disk
    replay (drop_incomplete degrades to False) — dropping it with nothing to re-supply
    it is exactly the incident's amputation."""
    orch, client = open_turn_edge
    # A transcript whose trailing user entry belongs to the OPEN turn: stamped in the
    # future so _is_in_progress_user would trim it if the drop were (wrongly) kept.
    open_turn_ts = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "first prompt"}}) + "\n"
        + json.dumps({"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T00:00:05Z",
                      "message": {"model": "m1", "content": [{"type": "text", "text": "done"}]}}) + "\n"
        + json.dumps({"type": "user", "uuid": "u2", "timestamp": open_turn_ts,
                      "message": {"role": "user", "content": "open turn prompt"}}) + "\n")
    from server.sessions.remote import RemoteDriver
    with mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=str(jsonl))), \
         mock.patch.object(RemoteDriver, "prepare_open_turn_resupply",
                           new=mock.AsyncMock(return_value=False)) as prep:
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1"})
            evB = _collect_until(
                wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    prep.assert_awaited()
    assert _count(evB, "replayComplete") == 1, _types(evB)
    # Degraded = NOT dropped: the open turn's user entry survives the disk replay.
    users = [e.get("text") for e in evB if e.get("type") == "user"]
    assert "open turn prompt" in users, evB
    assert "first prompt" in users, evB
