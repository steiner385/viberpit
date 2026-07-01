# In-progress turn replay — fixing truncated transcripts on re-subscribe

**Date:** 2026-07-01
**Status:** Approved (design)

## Problem

Conversation transcripts sometimes render only the *tail* of an assistant message
(e.g. a message that shows just `ate).`). It happens intermittently on live sessions.

### Root cause — two layers, one shape

The only durable record of a session is the Claude SDK's transcript JSONL file, and it
contains only **completed** turns. Every live event subscription starts "from now."
So whenever the live event stream is **re-subscribed while a turn is still streaming**,
the already-emitted portion of that in-progress turn is lost: it is not yet on disk, and
the fresh subscription only sees events emitted after it attached. The client renders the
tail-only remainder.

Two deployment modes exhibit this via different mechanisms:

- **In-process** (`ws.py` → real `SessionDriver.events()`): `driver._out` is a single
  *destructive* `asyncio.Queue`. A new `events()` iterator (created on every WS reconnect,
  or by a second viewer that splits the one queue) only receives events put on the queue
  after it starts awaiting. Deltas already drained by a previous/other consumer are gone.
  `SessionDriver` retains `_last_assistant_parts`, but that is used only to seed idle
  suggestions and is never replayed to a client.

- **Remote/daemon** (`ws.py` → `RemoteDriver.events()` → daemon topic): a single long-lived
  `_pump` (`orchestrator_rpc.py`) consumes `driver.events()` once and republishes to a
  per-session topic with a 256-entry replay ring (`daemon.py`). The driver-level queue is
  therefore *not* the problem here. Instead, `RemoteDriver.events()` subscribes
  `since=head_seq` where `head_seq` is captured **at attach time**
  (`remote.py`, `orchestrator_rpc.ensure_driver`). That is a from-now subscription, so the
  in-progress turn (published before attach) is skipped.

### Why "sometimes"

The first-ever viewer of a freshly created session is fine: the initial prompt is
deferred until the first attach, so the subscription is already live when the turn runs.
The bug only manifests on **reconnect** (mobile network blips, laptop sleep, redeploy/drain)
and on **second viewers** (desktop + phone, two tabs) — exactly the intermittent pattern
reported.

## Design

The fix gives the authoritative producer a bounded, replayable record of the current turn,
and makes each mode backfill it on attach through its natural mechanism — coordinated at a
single, mode-agnostic seam in `ws.py`.

### Layer 1 — driver (in-process)

1. **`_turn_buffer`** — a coalesced, block-structured record of the current turn on
   `SessionDriver`: the ordered blocks of the turn (user message; assistant text blocks
   holding the *accumulated* text so far; thinking blocks; tool calls; tool results).
   - Reset to empty at `_start_turn` (before the `UserMessage` is emitted).
   - Cleared when the turn settles into the JSONL (the turn-settled branch of `_drain`'s
     `finally`). Clearing at settle — not at next-turn-start — is what prevents a completed,
     already-on-disk turn from being shown twice (JSONL + buffer).
   - Coalesced per block (text appended into the open text block) so the buffer is bounded
     by turn **content**, not delta **count**. A 5-minute reply is a handful of blocks.

2. **`live_turn_events() -> list[dict]`** — returns `_turn_buffer` as wire-format events the
   existing frontend reducer already understands. A coalesced text block is emitted as a
   single `TextDelta` carrying the full accumulated text; after the client's `replayReset`
   the reducer builds the block from that one delta, and subsequent live deltas append to it.

3. **Broadcast fan-out** — replace the single destructive `_out` queue with per-subscriber
   fan-out:
   - `self._subscribers: set[asyncio.Queue]`.
   - `_emit(e)` (replacing the ~15 `await self._out.put(e)` call sites): appends `e` to
     `_turn_buffer` when `e` is transcript content, then `put_nowait`s `e` to every
     subscriber queue. Non-blocking; unbounded queues (parity with today's `out_q`).
   - `events()` creates a fresh queue, registers it in `_subscribers` **synchronously**
     (no `await` between registration and first drain, so no event can interleave-and-miss),
     drains it, and unregisters in `finally`.
   - `aclose()` broadcasts the `_STREAM_DONE` sentinel to every subscriber queue.
   - Two simultaneous in-process viewers now each receive the full stream.

   *Transcript-content classification:* only events that reconstruct the turn are buffered
   (`UserMessage`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`, `TurnEnd`,
   turn-scoped `ErrorEvent`). Control/ephemeral events (`Status`, `QueueUpdate`,
   `ContextUsage`, `Suggestions`, `Title`, `PermissionRequest`, `Question`, `Condensing`,
   `TurnFailure`) are **not** buffered — they are re-emitted from authoritative
   orchestrator/registry state by the existing `_emit_live_extras` path on attach, so
   buffering them would double-emit.

### Layer 2 — remote/daemon

4. **Subscribe from turn start, not from now.** The daemon's `_pump` tracks a
   `turn_start_seq` per stream, reset when it publishes a `UserMessage`. `ensure_driver`
   returns `turn_start_seq`; `RemoteDriver.events()` subscribes `since=turn_start_seq - 1`
   so the daemon replays the whole in-progress turn from its existing ring buffer.
   - Bump the default replay-ring size (`CCSWITCH_IPC_REPLAY_BUFFER`) so a normal turn fits.
   - Overflow (a turn longer than the ring) falls back to the existing `gap_frame` →
     edge-resync-from-disk path. That is the pre-existing worst case, now narrowed to only
     turns that exceed the ring rather than every reconnect.

### Uniform seam (`ws.py`)

After the JSONL replay + `replayComplete`, and before starting the live stream, emit
`driver.live_turn_events()` as one batch, then start the live stream.

- **In-process:** `live_turn_events()` returns the buffer (the in-progress turn); the live
  broadcast then carries only future events. Past = buffer, future = queue, no overlap.
- **Remote:** `RemoteDriver.live_turn_events()` returns `[]`. Remote backfill is owned by the
  single daemon ring via `since=turn_start_seq`, so emitting the buffer here too would
  duplicate. One source of truth per mode; the `ws.py` call site stays mode-agnostic.

## Why this avoids duplication

The dangerous seam is the distributed snapshot: separately snapshotting a driver-side turn
buffer *and* a daemon-side `head_seq` can overlap by one not-yet-pumped event. By making
`RemoteDriver.live_turn_events()` return `[]` and letting the single daemon ring own remote
backfill (`since=turn_start_seq`), each mode has exactly one source of truth for the
in-progress turn, and the `ws.py` seam never double-counts.

## Testing

- **Driver unit tests:**
  - Attach mid-turn (new `events()` subscriber) replays the coalesced in-progress content,
    then receives subsequent live deltas exactly once.
  - Two concurrent subscribers each receive the full stream (broadcast).
  - `_turn_buffer` is cleared at turn settle → attach while idle replays nothing (no dup
    against JSONL).
  - `live_turn_events()` emits a single `TextDelta` per coalesced text block, in block order.
- **Daemon test:** with a mid-turn stream, a subscriber using `since=turn_start_seq-1`
  replays the whole turn from the ring; `turn_start_seq` resets on each `UserMessage`.
- **Integration:** simulate attach → stream partial turn → reattach; assert the reattached
  client sees the full message, not the tail.

## Known limitations

- **Turn-boundary flush race:** clearing `_turn_buffer` at settle assumes the SDK has flushed
  the completed turn to the JSONL by then. A sub-second window at the exact boundary could
  miss the just-completed turn on a coincident attach; it self-heals on the next attach/refresh.
  This replaces total in-progress-turn loss with a narrow boundary window — a strict improvement.
- **Remote long-turn overflow:** a single turn longer than the (bumped) replay ring falls back
  to disk resync, which lacks the in-progress turn. Bounded by ring size; acceptable for v1.

## Out of scope

- Persisting in-progress turns to disk (would remove the flush-race window entirely but is a
  larger change to the SDK-owned transcript path).
- Reworking the frontend reducer beyond what `live_turn_events()`'s existing wire shapes need.
