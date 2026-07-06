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
    return [e.get("type", "") for e in events]


def _count(events: list[dict], t: str) -> int:
    return sum(1 for e in events if e.get("type") == t)


def _daemon_epoch(client) -> str:
    """The daemon epoch the edge has reconciled (#497). Non-None after any live attach has
    connected the events reader — sent back as ``sinceEpoch`` so the same-epoch catch-up
    path (the common fast path) is exercised, not silently downgraded to a full replay."""
    ep = client.app.state.sessions.daemon_epoch()
    assert ep is not None, "edge should have reconciled the daemon epoch after a live attach"
    return ep


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
    epoch = _daemon_epoch(client)

    # A separate socket re-attaches with a contiguous cursor at srcSeq 1 (the ring still
    # holds 0..3, so 2,3 are the missed tail) tagged with the CURRENT daemon epoch.
    with client.websocket_connect("/ws", headers=_IDENT) as wsB:
        wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                       "sinceEpoch": epoch})
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


def test_same_socket_reattach_with_cursor_catches_up(remote_edge):
    """Incident 2026-07-05, Hole 2 + catch-up: a re-attach on the SAME socket without an
    intervening detach (the session is still in this socket's live_tasks) must NOT be a
    silent no-op — it rebinds (tears down this socket's forwarding task) and re-runs the
    attach path. With a still-covered srcSeq cursor that means catch-up: a resumeOk and the
    missed ring tail, NO replayReset/replayComplete, and the tail arrives exactly-once
    (single forwarding task after the rebind — no duplicate from a leaked old task)."""
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    driver = _RingDriver(4)
    with mock.patch.object(orch, "ensure_driver", new=mock.AsyncMock(return_value=driver)), \
         mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=None)):
        with client.websocket_connect("/ws", headers=_IDENT) as ws:
            # First attach drains the 4 streamed deltas (srcSeq 0..3); the ring holds 0..3.
            ws.send_json({"type": "attach", "sessionId": "s1"})
            ev1 = _collect_until(ws, lambda acc: _count(acc, "textDelta") >= 4)
            assert [e["srcSeq"] for e in ev1 if e.get("type") == "textDelta"] == [0, 1, 2, 3], ev1

            # SAME socket, NO detach: re-attach with a contiguous cursor at srcSeq 1, tagged
            # with the current daemon epoch (same-epoch catch-up — the common fast path).
            ws.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                          "sinceEpoch": _daemon_epoch(client)})
            ev2 = _collect_until(
                ws, lambda acc: any(e.get("type") == "resumeOk" for e in acc)
                and _count(acc, "textDelta") >= 2)

    assert _count(ev2, "resumeOk") == 1, _types(ev2)
    # Catch-up path after the rebind: NEITHER full-replay bracket.
    assert _count(ev2, "replayReset") == 0, _types(ev2)
    assert _count(ev2, "replayComplete") == 0, _types(ev2)
    # The missed tail: exactly srcSeq 2,3, exactly once each, in order.
    tail = [e for e in ev2 if e.get("type") == "textDelta"]
    assert [e["srcSeq"] for e in tail] == [2, 3], ev2
    assert [e["text"] for e in tail] == ["d2", "d3"], ev2


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
    epoch = _daemon_epoch(client)
    with mock.patch.object(sessions, "ring_covers", new=mock.AsyncMock(return_value=False)):
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                           "sinceEpoch": epoch})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


def test_evict_between_probe_and_snapshot_falls_back_to_full_replay(live_ring_edge):
    """FINDING-3: the ring evicts in the window BETWEEN the ring_covers probe (still covered)
    and the ring_tail snapshot fetch. The commit-time coverage recheck (ring_tail) reports
    not-covered, so NO resumeOk is sent and the attach falls back to the full disk replay
    (replayComplete) — the FINDING-1 atomic-commitment guarantee: resumeOk is only ever
    enqueued with a covered snapshot in hand, never ahead of a silently-evicted tail.

    Faked at the ring_tail / coverage-recheck level (NOT the probe level): ring_covers is
    left REAL — it genuinely returns covered for cursor 1 (the ring holds seq 0..3) — while
    ring_tail returns not-covered, exactly modelling an eviction landing in that window."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    epoch = _daemon_epoch(client)
    not_covered = mock.AsyncMock(return_value={"covered": False, "events": [], "head": 3})
    with mock.patch.object(sessions, "ring_tail", not_covered):
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                           "sinceEpoch": epoch})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    not_covered.assert_awaited()  # the commit-time recheck actually ran (probe alone isn't enough)
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
# Epoch gate (#497): a cursor is honored only when its minting epoch matches the daemon's
# CURRENT epoch. The gate runs BEFORE the ring_covers probe, so a mismatched/absent/unknown
# epoch full-replays without even consulting the ring.
# ---------------------------------------------------------------------------

def test_cursor_without_epoch_full_replays_and_never_probes(live_ring_edge):
    """A cursor sent with NO sinceEpoch (an old client, or an unknown epoch) is rejected by
    the epoch gate → full replay, and ring_covers is never consulted (the gate is BEFORE the
    probe)."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    with mock.patch.object(sessions, "ring_covers",
                           new=mock.AsyncMock(side_effect=AssertionError("must not probe"))) as spy:
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    spy.assert_not_awaited()
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


def test_cursor_with_mismatched_epoch_full_replays_and_never_probes(live_ring_edge):
    """A cursor whose sinceEpoch != the daemon's current epoch (a stale cross-epoch cursor)
    is rejected → full replay, ring_covers never consulted."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    with mock.patch.object(sessions, "ring_covers",
                           new=mock.AsyncMock(side_effect=AssertionError("must not probe"))) as spy:
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                           "sinceEpoch": "some-other-epoch"})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    spy.assert_not_awaited()
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


def test_unknown_edge_epoch_full_replays(live_ring_edge):
    """When the edge's known daemon epoch is None (e.g. /info never reached), be
    CONSERVATIVE: any cursor is a mismatch → full replay, never a catch-up against an unknown
    epoch."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    with mock.patch.object(sessions, "daemon_epoch", return_value=None), \
         mock.patch.object(sessions, "ring_covers",
                           new=mock.AsyncMock(side_effect=AssertionError("must not probe"))) as spy:
        with client.websocket_connect("/ws", headers=_IDENT) as wsB:
            wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                           "sinceEpoch": "E1"})
            evB = _collect_until(wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    spy.assert_not_awaited()
    assert _count(evB, "replayComplete") == 1, _types(evB)
    assert _count(evB, "resumeOk") == 0, _types(evB)


def test_resumeOk_and_replayComplete_carry_epoch_in_remote_mode(live_ring_edge):
    """The frames the browser uses to establish a cursor carry the daemon `epoch` in remote
    mode: resumeOk on the covered catch-up path, replayComplete on the full-replay path."""
    orch, client = live_ring_edge
    sessions = client.app.state.sessions
    epoch = _daemon_epoch(client)

    # Covered catch-up → resumeOk stamped with the epoch.
    with client.websocket_connect("/ws", headers=_IDENT) as wsB:
        wsB.send_json({"type": "attach", "sessionId": "s1", "sinceSrcSeq": 1,
                       "sinceEpoch": epoch})
        evB = _collect_until(
            wsB, lambda acc: any(e.get("type") == "resumeOk" for e in acc))
    resume = next(e for e in evB if e.get("type") == "resumeOk")
    assert resume.get("epoch") == epoch, resume

    # Full replay (no cursor) → replayComplete stamped with the epoch.
    with client.websocket_connect("/ws", headers=_IDENT) as wsC:
        wsC.send_json({"type": "attach", "sessionId": "s1"})
        evC = _collect_until(wsC, lambda acc: any(e.get("type") == "replayComplete" for e in acc))
    complete = next(e for e in evC if e.get("type") == "replayComplete")
    assert complete.get("epoch") == epoch, complete


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
    # Local mode has no daemon epoch, so replayComplete carries no `epoch` field (#497) —
    # mirrors how srcSeq is omitted locally.
    complete = next(e for e in events if e.get("type") == "replayComplete")
    assert "epoch" not in complete, complete
    client.portal.call(orch.close_session, sid)


# ---------------------------------------------------------------------------
# FINDING-2: the snapshot↔live overlap dedup (`src <= head`) exercised directly. An event
# emitted AFTER the subscriber joined the fan-out but INSIDE the snapshot range arrives via
# BOTH the ring_tail snapshot AND the live fan-out — the dedup must deliver it exactly once.
# The boundary case seq == head is the one `<=` (correct) vs `<` (bug) disagree on, so this
# fails under that mutation (which otherwise passed the whole suite).
# ---------------------------------------------------------------------------

class _OverlapClient:
    """Fake DaemonClient: subscribe() replays a preset live fan-out carrying daemon seqs
    (via _SeqDict); the ring_tail snapshot is supplied out-of-band through pin_catch_up, so
    RemoteDriver's snapshot-then-dedup-live path is exercised in isolation."""

    def __init__(self, live_frames):
        self._live_frames = live_frames

    def subscribe(self, session_id, since=None):
        async def _gen():
            for f in self._live_frames:
                yield f
        return _gen()


@pytest.mark.asyncio
async def test_catch_up_dedups_snapshot_and_live_overlap_exactly_once():
    from server.ipc.client import _SeqDict
    from server.sessions.remote import RemoteDriver

    # The live fan-out (joined BEFORE the snapshot was pinned) re-delivers seq 3 — which is
    # ALSO the snapshot's last event (head == 3) — plus a genuinely-new seq 4 past the head.
    live = [_SeqDict({"type": "textDelta", "text": "d3"}, 3),
            _SeqDict({"type": "textDelta", "text": "d4"}, 4)]
    d = RemoteDriver(_OverlapClient(live), "s1")
    d.join_live()  # join the fan-out first (atomic-commitment ordering)
    d.pin_catch_up(
        [{"seq": 2, "event": {"type": "textDelta", "text": "d2"}},
         {"seq": 3, "event": {"type": "textDelta", "text": "d3"}}],
        head=3,
    )
    got = [e async for e in d.events()]
    # seq 3 is in BOTH the snapshot and the live fan-out; the `src <= head` dedup must drop
    # the live copy. Exactly-once by srcSeq AND by content, in order. Mutating `<=` to `<`
    # re-admits the seq==head live frame → a duplicate d3 → [2, 3, 3, 4] → this fails.
    assert [e.src_seq for e in got] == [2, 3, 4]
    assert [e.to_wire()["text"] for e in got] == ["d2", "d3", "d4"]


# ---------------------------------------------------------------------------
# #497 — cross-epoch cursor after an ACTUAL daemon restart. The daemon srcSeq space is
# per-process (a restart resets it to 0). A browser disconnected across the restart holds a
# stale high `sinceSrcSeq` from the OLD epoch; if the new epoch has already produced events
# past it while the ring still holds it, the old `ring_covers` would return True and catch-up
# would SILENTLY SKIP the new epoch's early events. The echo-epoch/edge-reject fix forces a
# full replay because the echoed `sinceEpoch` no longer matches the daemon's current epoch.
#
# This restarts a REAL daemon on the SAME socket (the test_ipc_daemon_restart_gap mechanism)
# under a live remote edge, so the DaemonClient genuinely reconciles the new epoch.
# ---------------------------------------------------------------------------

class _RestartableThreadDaemon:
    """A daemon app on a FIXED uds in its own thread/loop, restartable on the SAME socket.
    Each start mints a fresh ``create_daemon_app`` → a NEW epoch and a seq space reset to 0,
    exactly as a production daemon restart. Bound to a real socket so the edge (its own loop)
    reaches it cross-thread and its reader reconnects + reconciles the epoch."""

    def __init__(self, orch, sock):
        import threading
        self._orch = orch
        self.sock = sock
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._err = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import uvicorn
        from server.ipc.daemon import create_daemon_app
        from server.ipc.orchestrator_rpc import register_orchestrator_rpc
        from server.ipc.x11_rpc import register_x11

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            dapp = create_daemon_app()
            register_orchestrator_rpc(dapp, self._orch)
            register_x11(dapp, getattr(self._orch, "_x11", None))
            server = uvicorn.Server(uvicorn.Config(
                dapp, uds=self.sock, log_level="error", lifespan="off"))
            stask = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.01)
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.02)
            server.should_exit = True
            await stask

        try:
            loop.run_until_complete(_serve())
        except Exception as exc:  # noqa: BLE001 — surface to the main thread
            self._err = exc
            self._ready.set()
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def start(self):
        self._t.start()
        if not self._ready.wait(5):
            raise RuntimeError("daemon thread did not start")
        if self._err is not None:
            raise self._err

    def stop(self):
        import contextlib
        import os
        self._stop.set()
        self._t.join(5)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.sock)


class _FiniteRingDriver(_RingDriver):
    """Like _RingDriver but events() publishes ``n`` deltas then RETURNS (no blocking wait).

    The blocking variant keeps the stream open so a SECOND attach can catch up from the same
    daemon's ring — but across a daemon RESTART each attach hits a fresh daemon anyway, and
    the blocking generator is fragile under the restart's task teardown (it GC-aclose-races,
    so the pump can die before publishing). A finite generator publishes 0..n-1 into the new
    epoch's ring and cleans up, which is all the cross-epoch full-replay assertion needs."""

    async def events(self):
        for i in range(self._n):
            yield _RingEvent(i)


def _fresh_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._records["s1"] = _rec("s1")
    return orch


def test_cross_epoch_cursor_after_daemon_restart_forces_full_replay(tmp_path):
    import contextlib
    import os

    from starlette.testclient import TestClient

    from server.app import create_app

    # A daemon restart is a NEW process: daemon B gets its OWN orchestrator (a fresh asyncio
    # loop backs each daemon thread — reusing one orchestrator's loop-bound async state across
    # both loops would wedge). The edge (app.state.sessions) is stable across the restart and
    # simply reconnects to whatever daemon now owns the socket.
    orch_a = _fresh_orch()
    orch_b = _fresh_orch()
    sock = str(tmp_path / "d.sock")
    daemon = _RestartableThreadDaemon(orch_a, sock)
    daemon.start()
    daemon2 = None
    env = {
        "CCSWITCH_SESSIOND_SOCKET": sock,
        "CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}",
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                sessions = client.app.state.sessions
                # ---- Epoch E1: first attach establishes a cursor (srcSeq 0..3). ----
                with mock.patch.object(orch_a, "ensure_driver",
                                       new=mock.AsyncMock(return_value=_FiniteRingDriver(4))), \
                     mock.patch.object(orch_a, "transcript_path",
                                       new=mock.AsyncMock(return_value=None)):
                    with client.websocket_connect("/ws", headers=_IDENT) as wsA:
                        wsA.send_json({"type": "attach", "sessionId": "s1"})
                        evA = _collect_until(wsA, lambda acc: _count(acc, "textDelta") >= 4)
                        assert [e["srcSeq"] for e in evA
                                if e.get("type") == "textDelta"] == [0, 1, 2, 3], evA
                epoch1 = sessions.daemon_epoch()
                assert epoch1 is not None

                # ---- RESTART the daemon on the SAME socket (new epoch, seq → 0). ----
                daemon.stop()
                daemon2 = _RestartableThreadDaemon(orch_b, sock)
                daemon2.start()
                # Wait for the edge's reader to reconnect and reconcile the new epoch.
                deadline = _time.time() + 5
                while _time.time() < deadline and sessions.daemon_epoch() == epoch1:
                    _time.sleep(0.05)
                epoch2 = sessions.daemon_epoch()
                assert epoch2 is not None and epoch2 != epoch1, (epoch1, epoch2)

                # ---- Epoch E2: re-attach with the STALE cursor (srcSeq 3) + STALE epoch. ----
                # The full replay for the NEW epoch reads the DURABLE disk transcript (the
                # real full-replay source of truth — NOT the ring), so the new epoch's early
                # events are supplied by a real transcript file here. A live-but-empty driver
                # keeps the attach a valid live session without racing the ring.
                transcript = tmp_path / "s1.jsonl"
                transcript.write_text("\n".join(_json.dumps(o) for o in [
                    {"type": "user", "uuid": "u0", "message": {"content": "q0"}},
                    {"type": "assistant", "uuid": "a0",
                     "message": {"model": "claude-x",
                                 "content": [{"type": "text", "text": "r0"}],
                                 "usage": {"input_tokens": 1}}},
                ]))
                with mock.patch.object(orch_b, "ensure_driver",
                                       new=mock.AsyncMock(return_value=_FiniteRingDriver(0))), \
                     mock.patch.object(orch_b, "transcript_path",
                                       new=mock.AsyncMock(return_value=str(transcript))):
                    with client.websocket_connect("/ws", headers=_IDENT) as wsB:
                        wsB.send_json({"type": "attach", "sessionId": "s1",
                                       "sinceSrcSeq": 3, "sinceEpoch": epoch1})
                        evB = _collect_until(
                            wsB, lambda acc: any(e.get("type") == "replayComplete" for e in acc))

                # Stale cross-epoch cursor rejected → FULL replay (replayReset + replayComplete),
                # NOT resumeOk.
                assert _count(evB, "resumeOk") == 0, _types(evB)
                assert _count(evB, "replayReset") == 1, _types(evB)
                assert _count(evB, "replayComplete") == 1, _types(evB)
                # The new epoch's events are actually replayed (NOT silently skipped by a
                # cross-epoch catch-up): the transcript's turn shows up between the brackets.
                assert _count(evB, "turnStart") >= 1, _types(evB)
    finally:
        with contextlib.suppress(Exception):
            daemon.stop()
        if daemon2 is not None:
            with contextlib.suppress(Exception):
                daemon2.stop()
