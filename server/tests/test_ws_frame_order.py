"""WS attach frame-ordering tests — the single-FIFO transport contract.

Two bug shapes are pinned here:

* B4 "stale-straddle": live events flow driver → out_q → batcher, but the replay
  frames used to be _send-ed DIRECTLY. On a detach→attach (exactly what navigation
  and the client's gap-heal do), pre-detach frames still parked in out_q / the
  batcher's buffer were flushed AFTER the fresh replayReset — same sessionId, so
  no client filter dropped them. Their stale old-epoch ``seq`` then poisoned the
  client's freshly-reset gap baseline (the new live stream restarts at seq 0 →
  looks like a gap → detach+attach heal loop that wipes/rebuilds the transcript
  while a turn streams). The fix routes EVERY session-scoped attach frame through
  out_q, so one FIFO defines the total order.

* B3 "settle-race": _handle_attach used to read driver.turn_active, await the
  multi-step disk replay, and only then subscribe to the driver, whose snapshot
  copies _turn_buffer. If the turn settled during those awaits the buffer was
  cleared → empty snapshot → the in-progress turn that the replay had dropped
  (drop_incomplete=True) was delivered by NOBODY. driver.open_stream() now
  captures the snapshot + turn flags atomically BEFORE the replay.
"""
from __future__ import annotations

import asyncio
import json
import time

import server.bridge.ws as wsmod
from fastapi.testclient import TestClient
from server.app import create_app

_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


class _FakeEvent:
    def __init__(self, wire):
        self._wire = wire

    def to_wire(self):
        return dict(self._wire)


class _Reg:
    def __init__(self, pending=None):
        self._pending = pending or []

    def pending(self, sid):
        return list(self._pending)


class _BaseOrch:
    """Minimal orchestrator stub covering the WS bridge's attach surface."""

    def __init__(self, driver, path=None, registry=None):
        self._drivers: dict = {}
        self._registry = registry or _Reg()
        self._driver = driver
        self._path = path

    async def rehydrate(self): pass
    async def start(self): pass
    async def stop(self): pass
    def subscribe_events(self): return asyncio.Queue()
    def unsubscribe_events(self, q): pass
    async def ensure_driver(self, session_id, identity): return self._driver
    async def get_session(self, session_id): return None
    async def transcript_path(self, session_id): return self._path
    # The attach's fallback when there is no transcript FILE (an engine whose
    # history lives behind the daemon). None here = "no history to replay", which
    # is what these frame-ordering tests want: they assert on the reset/permission
    # ordering, not on history.
    async def engine_replay_window(self, session_id, *, drop_incomplete_turn=False,
                                   turn_started_at=None): return None
    async def command_catalog(self, session_id): return []
    def pending_questions(self, session_id): return []
    def pop_pending_initial_prompt(self, session_id): return None
    def pop_pending_initial_attachment_ids(self, session_id): return None


class _BlockingStream:
    """A driver stream that yields a scripted prefix then blocks forever (live)."""

    def __init__(self, prefix, turn_active=False, turn_started_at=None):
        self._prefix = prefix
        self.turn_active = turn_active
        self.turn_started_at = turn_started_at

    async def events(self):
        for e in self._prefix:
            yield e
        await asyncio.Event().wait()

    def close(self):
        pass


class _ScriptedDriver:
    """First attach streams 'stale' pre-detach events; later attaches stream nothing."""

    identity = "wstest@example.com"

    def __init__(self):
        self.attach_count = 0

    def current_status(self): return "idle"
    def queued(self): return []
    def queue_wire(self): return []
    def seed_idle_suggestions(self, user_text, assistant_text): pass

    def open_stream(self):
        self.attach_count += 1
        if self.attach_count == 1:
            prefix = [
                _FakeEvent({"type": "textDelta", "turnId": "t-old", "text": "stale-1"}),
                _FakeEvent({"type": "textDelta", "turnId": "t-old", "text": "stale-2"}),
            ]
        else:
            prefix = []
        return _BlockingStream(prefix)

    async def events(self):
        # Pre-fix compatibility path (ws.py used driver.events() directly).
        async for e in self.open_stream().events():
            yield e

    @property
    def turn_active(self): return False

    @property
    def turn_started_at(self): return None


def _write_transcript(tmp_path):
    rows = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "first-r"}]}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _collect_flat(ws, done, timeout=8.0):
    """Receive frames, flattening batches IN ORDER, until done(flat) or timeout.

    Frame boundaries are preserved separately in the second return value so tests
    can assert batch-vs-top-level delivery.
    """
    flat: list[dict] = []
    raw: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = json.loads(ws.receive_text())
        except Exception:
            break
        raw.append(msg)
        if msg.get("type") == "batch":
            flat.extend(msg.get("events", []))
        else:
            flat.append(msg)
        if done(flat):
            break
    return flat, raw


# ---------------------------------------------------------------------------
# B4 — one FIFO across the detach→attach boundary
# ---------------------------------------------------------------------------

def test_detach_attach_stale_frames_never_land_after_the_fresh_reset(tmp_path, monkeypatch):
    """Stale-straddle: events from the OLD stream epoch still buffered when the
    client detaches must be delivered BEFORE the new attach's replayReset — never
    after it. A slow batcher interval parks the stale events exactly the way a
    16ms flush window does under load; the direct-send replay used to overtake
    them."""
    # Widen the batcher window so the stale live events are still parked in
    # out_q/buf when the detach→attach runs (deterministic straddle).
    monkeypatch.setattr(wsmod, "_BATCH_INTERVAL", 0.5)

    path = _write_transcript(tmp_path)
    driver = _ScriptedDriver()
    app = create_app(orchestrator=_BaseOrch(driver, path=path))

    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            # Immediately flip: the stale events are enqueued by the live task but
            # the batcher is mid-sleep — they must NOT be overtaken by the fresh
            # replay of the re-attach.
            ws.send_text(json.dumps({"type": "detach", "sessionId": "s1"}))
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))

            def done(flat):
                completes = [e for e in flat if e.get("type") == "replayComplete"]
                stales = [e for e in flat if e.get("text") == "stale-2"]
                return len(completes) >= 2 and len(stales) >= 1

            flat, _raw = _collect_flat(ws, done)

    types = [(e.get("type"), e.get("text")) for e in flat]
    resets = [i for i, e in enumerate(flat) if e.get("type") == "replayReset"]
    completes = [i for i, e in enumerate(flat) if e.get("type") == "replayComplete"]
    stale_idx = [i for i, e in enumerate(flat) if e.get("text") in ("stale-1", "stale-2")]
    assert len(resets) >= 2 and len(completes) >= 2 and len(stale_idx) == 2, (
        f"expected two attach cycles plus both stale events; got {types}")
    second_reset = resets[1]
    # THE assertion: no old-epoch frame may follow the fresh reset.
    assert all(i < second_reset for i in stale_idx), (
        f"stale pre-detach events landed AFTER the fresh replayReset "
        f"(they would duplicate content and poison the seq baseline): {types}")
    # And the fresh replay retains its internal order: reset → replay → complete.
    replay_users_after = [i for i, e in enumerate(flat)
                          if e.get("type") == "user" and i > second_reset]
    assert replay_users_after and all(i < completes[1] for i in replay_users_after), (
        f"replay events must sit between the fresh reset and its replayComplete: {types}")


# ---------------------------------------------------------------------------
# B3 — the attach snapshot must survive a turn settling during the replay await
# ---------------------------------------------------------------------------

def test_attach_delivers_in_progress_turn_even_if_it_settles_during_replay(monkeypatch):
    """Settle-race: the driver reports turn_active with a populated _turn_buffer;
    the replay path is slow and the turn settles (buffer cleared, turn_active
    flipped) before it returns. The attach must still deliver the in-progress
    turn's events — open_stream() captured them before the replay awaited."""
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions import events as ev
    from server.sessions.permissions import PermissionRegistry

    driver = ClaudeAgentDriver(
        "s1", "acct", "/tmp", "/tmp/cfg", mode="interactive",
        sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(),
        identity="wstest@example.com")
    driver._turn_active = True
    driver._turn_started_at = time.time()
    driver._turn_buffer = [ev.TextDelta(turn_id="t1", text="in-progress-tail")]

    class _Window:
        events: list = []
        earliest_turn = 0
        total_turns = 0

    async def settling_replay(path, **kw):
        # Simulate the CLI settling the turn while the attach awaits the replay:
        # the drain's finally clears the buffer and flips the flags.
        await asyncio.sleep(0.05)
        driver._turn_buffer = []
        driver._turn_active = False
        driver._turn_started_at = None
        return _Window()

    monkeypatch.setattr(wsmod, "replay_window", settling_replay)

    app = create_app(orchestrator=_BaseOrch(driver, path="whatever.jsonl"))
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))

            def done(flat):
                return any(e.get("text") == "in-progress-tail" for e in flat)

            flat, _raw = _collect_flat(ws, done, timeout=4.0)

    types = [e.get("type") for e in flat]
    tail_idx = [i for i, e in enumerate(flat) if e.get("text") == "in-progress-tail"]
    assert tail_idx, (
        f"the in-progress turn settled during the replay and its events were lost "
        f"(nobody delivered the snapshot); got {types}")
    complete_idx = types.index("replayComplete")
    assert tail_idx[0] > complete_idx, (
        "the snapshot replay must follow replayComplete (live-stream epoch)")


# ---------------------------------------------------------------------------
# Extras ordering — session-scoped attach extras ride the FIFO, after the reset
# ---------------------------------------------------------------------------

def test_pending_permission_reemit_arrives_batched_after_replay_reset():
    """A pending permission re-emitted on attach must arrive AFTER replayReset and
    inside a batch (sessionEvent path) — a direct top-level send could overtake the
    out_q replay, and the reducer stores permission blocks inside turns, so a
    permissionRequest landing before the reset is wiped by it."""
    pending = [{"permId": "perm-1", "turn_id": "t1", "tool": "ExitPlanMode",
                "input": {"plan": "ship it"}}]
    driver = _ScriptedDriver()
    # Mid-turn attach: turn_active=True forces the replayReset even with no history.
    driver.open_stream = lambda: _BlockingStream(  # type: ignore[method-assign]
        [], turn_active=True, turn_started_at=time.time())
    app = create_app(orchestrator=_BaseOrch(driver, path=None, registry=_Reg(pending)))

    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))

            def done(flat):
                return any(e.get("type") == "permissionRequest" for e in flat)

            flat, raw = _collect_flat(ws, done, timeout=4.0)

    # Never a top-level permissionRequest frame: it must ride the batch path so
    # ordering vs the replay is defined by the FIFO, and the client's sessionId
    # filter applies (handleLiveBatch), not the legacy top-level fan-out.
    assert not any(m.get("type") == "permissionRequest" for m in raw), (
        f"permissionRequest must be batched, not top-level: {[m.get('type') for m in raw]}")
    types = [e.get("type") for e in flat]
    assert "permissionRequest" in types, f"no permissionRequest re-emit in {types}"
    reset_idx = types.index("replayReset")
    perm_idx = types.index("permissionRequest")
    complete_idx = types.index("replayComplete")
    assert reset_idx < complete_idx < perm_idx, (
        f"extras must follow the replay they annotate (reset@{reset_idx} < "
        f"complete@{complete_idx} < permission@{perm_idx}): {types}")
    perm = flat[perm_idx]
    assert perm.get("sessionId") == "s1", "extras frames must be session-tagged"
    assert perm.get("plan") is True

    # The live Status extra must also be batched — the legacy top-level
    # {"type":"status","state":...} frame collided with the client's lifecycle
    # 'status' handler ({sessionId,status}) and never reached the transcript
    # reducer (B5). Batched, it is dispatched as a sessionEvent.
    assert not any(m.get("type") == "status" and "state" in m for m in raw), (
        "live Status must not be a top-level frame")
    status_batched = [e for e in flat if e.get("type") == "status" and "state" in e]
    assert status_batched and status_batched[0].get("sessionId") == "s1"
