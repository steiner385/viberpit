# Session daemon — A3: event replay across the attach/reconnect gap

**Date:** 2026-06-22
**Status:** Approved (design; self-driven under an autonomous roadmap directive)
**Phase:** A3. Builds on A2.2b-2b (remote facade live-but-dormant). Hardens the remote
streaming path before A2.3 flips the default to remote.

## Why

The daemon starts a per-session event pump on `ensure_driver`; the edge subscribes a beat
later over the `/events` WS. The daemon's `publish` drops frames that have no current
subscriber (`server/ipc/daemon.py`), so any event emitted in that window — or during a WS
reconnect — is lost with no replay. Documented and deferred since A2.2b-1. Benign while a
session is idle at attach, but a real correctness hole once remote mode is the default and a
prompt is mid-flight during (re)attach. A3 closes it.

## Design

A bounded **per-stream ring buffer keyed by a monotonic sequence number**, with the subscriber
declaring the point it has already accounted for (`since`) so the daemon replays exactly the
missed events — no gap, no duplication of persisted history.

### The duplication trap (why naive replay is wrong)

`ws.py` already replays the persisted **transcript** on every attach (a `replayReset` batch),
covering all history up to the attach point. The live stream must cover only events *after*
that point. A naive "replay the whole buffer on subscribe" would re-deliver live deltas that
are also in the transcript → the session-content-duplication bug. The fix is to anchor replay
at the attach point: the subscriber asks for events *after* a sequence it has already covered.

### Sequence anchor

- **First attach:** `ensure_driver` returns the stream's current `head_seq` (max seq emitted so
  far, or `-1`). Everything `≤ head_seq` is already in the transcript the edge replays, so the
  edge subscribes with `since = head_seq`. The daemon replays only `seq > head_seq` — exactly
  the gap-window events emitted between `ensure_driver` and the subscribe handshake.
- **Reconnect:** the client tracks the highest seq it has delivered per stream and re-subscribes
  with `since = last_delivered_seq`. The daemon replays only what was missed during the drop.

Both cases: `since` is "the point up to which the client has already accounted for events." No
gap, no dup.

### Daemon (`server/ipc/daemon.py`)

- `app.state.buffers: dict[str, collections.deque]` — per-stream ring of `(seq, event)`,
  `maxlen = CCSWITCH_IPC_REPLAY_BUFFER` (default 256). Bounded memory; oldest dropped.
- `app.state.seqs: dict[str, int]` — per-stream monotonic counter (starts at -1; first event 0).
- `publish(stream, event)`: assign `seq = ++seqs[stream]`; append `(seq, event)` to the buffer;
  send `event_frame(stream, event, seq)` to every current subscriber. (Buffering happens
  regardless of subscribers — that is what closes the gap.)
- `/events` SUBSCRIBE handling: the subscribe frame may carry `since` (int or null). On
  subscribe, register the WS, then immediately replay buffered `(seq, event)` with `seq > since`
  (or all buffered if `since` is null) to *that* WS as `event_frame`s, in order, before any live
  frame. Replaying only to the newly-subscribed WS (not the whole set) avoids disturbing others.
- A helper `head_seq(stream) -> int` returns `seqs.get(stream, -1)`.

### Protocol (`server/ipc/protocol.py`)

- `event_frame(stream, event, seq)` → `{"type": EVENT, "stream", "event", "seq"}`.
- `subscribe_frame(stream, since=None)` → `{"type": SUBSCRIBE, "stream", "since"}`.
  (Both additive; a missing field is treated as null/auto. Edge and daemon deploy together;
  cross-version safety is A4's handshake.)

### RPC (`server/ipc/orchestrator_rpc.py`)

- `ensure_driver` response gains `head_seq = app.state.head_seq(session_id)` (alongside the
  existing `found`/`status`/`queued`).
- The per-session `_pump` uses `app.state.publish` unchanged (publish now assigns seq + buffers).

### Client (`server/ipc/client.py`)

- `self._last_seq: dict[str, int]` — highest delivered seq per stream.
- `subscribe(stream, since=None)`: record an initial `since` baseline for the stream; the reader
  and `_send_subscribe` send `subscribe_frame(stream, since=self._last_seq.get(stream, <baseline>))`.
- `_deliver(stream, frame)`: read `seq` from the frame, set `self._last_seq[stream] = seq`, and
  enqueue **only the event dict** (seq stripped) so `RemoteDriver.events()` yields the same wire
  dict as today — `ws.py` is untouched.
- On reconnect, `_reader` re-subscribes every desired stream with its tracked `_last_seq` →
  replay only the missed tail.

### Remote facade (`server/sessions/remote.py`)

- `RemoteDriver(client, session_id, status, queued, head_seq)`: pass `head_seq` from the
  `ensure_driver` response; `events()` calls `client.subscribe(session_id, since=head_seq)` so the
  first subscribe is anchored at the attach point.
- `RemoteOrchestrator.ensure_driver` threads `r["head_seq"]` into the `RemoteDriver`.

## Data flow (remote attach, gap closed)

```
edge: ensure_driver  ──RPC──▶ daemon: {found,status,queued, head_seq=H}; pump already publishing+buffering
edge: subscribe(session, since=H)  ──▶ daemon replays buffer seq>H to this WS, then live
       (events emitted in the H→subscribe window are in the buffer → delivered, not dropped)
reconnect: subscribe(session, since=last_delivered)  ──▶ daemon replays only the missed tail
```

## Error handling / bounds

- Buffer is bounded (`maxlen`); under a flood, the oldest buffered events are dropped — a
  subscriber whose `since` predates the buffer's oldest entry gets the whole (truncated) buffer
  and a one-line daemon warning. This matches the existing client-side drop-oldest policy.
- `publish` keeps its dead-subscriber discard behavior.
- No change to `ws.py`, the orchestrator, or the edge app.

## Testing

Integration over the in-process daemon-on-UDS harness (as A2.x):
- **Gap closed:** start a pump, publish event(s) BEFORE subscribing, then subscribe with
  `since = -1` (or the pre-event head) → the pre-subscribe events are replayed in order.
- **No dup at the anchor:** publish N events, capture `head_seq`, subscribe with `since = head_seq`
  → none of the first N replay; a subsequently-published event IS delivered.
- **Reconnect tail:** subscribe, receive a few, drop the WS, publish more, reconnect (client
  re-subscribes with last-seen) → only the new events arrive, in order, no dup.
- **Bounded buffer:** publish > maxlen events, subscribe with `since=-1` → exactly the last
  `maxlen` replay (oldest dropped), with a warning.
- Unit: `event_frame`/`subscribe_frame` carry the new fields; `DaemonClient._deliver` strips seq
  and tracks `_last_seq`.
- Regression: the existing `test_remote_*`, `test_ipc_*`, and `test_remote_mode_edge` suites stay
  green (events still reach `ws.py` as bare dicts; the A2.2b-2b WS attach/stream test no longer
  needs its `sleep` crutch, but leave it — removing it is optional polish, not required).

## Out of scope

- The daemon production entrypoint / lifespan / systemd / version handshake (A4).
- Flipping the edge default to remote (A2.3).
- Any change to `server/bridge/ws.py`, the orchestrator, or the edge `create_app`/lifespan.
- Persisting the buffer across daemon restarts (the buffer is in-memory; a daemon restart is an
  A4 deploy-classification concern, not a replay concern).
