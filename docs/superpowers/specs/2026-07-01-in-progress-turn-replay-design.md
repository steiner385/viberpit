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

## Verified transcript behavior (spike, 2026-07-01)

Confirmed against the real `claude` binary (v2.1.197) — both an isolated headless run
polled mid-stream and a real ccswitch/SDK transcript:

- The CLI writes **one JSONL line per content block**, each with its own `entry.uuid`
  (blocks of one API message share `msg.id` but are separate lines).
- A **partial, still-streaming block is absent from disk** until it completes: in the
  spike the assistant text stayed 0 chars on disk for ~33s, then jumped to full (4589) in
  one step — never written incrementally.
- **Completed** blocks of an in-progress turn **are** flushed mid-turn (real transcript:
  block timestamps :16.7 / :17.1 / :18.8 spread across the turn, before it ended).
- The **user line is flushed at send, before any assistant output** (spike: user line on
  disk at t=3.9s while 0 assistant blocks existed; real transcript: user :09 vs assistant :16).

**Implication:** on a mid-stream reconnect the JSONL holds `[prior turns] + [current turn's
user line + its completed blocks]`, missing only the in-flight block. Naively replaying the
JSONL and then the driver buffer therefore duplicates the current turn's completed blocks.
Because the user line is reliably on disk before streaming, "exclude from the last real user
line" reliably identifies the in-progress turn with no flush-race in practice.

Live (translator) and replay (replay.py) also use different turn-id schemes at different
granularities (live: one stable `tid` per logical turn, reset at `ResultMessage`; replay:
one `TurnStart`/`TurnEnd` per block keyed by `entry.uuid`), so there is no shared key to
dedup the two sources. The stitch must make them cover **disjoint** logical turns.

## Design

The fix gives the authoritative producer a bounded, replayable record of the current turn,
and makes each mode backfill it on attach through its natural mechanism — coordinated at a
single, mode-agnostic seam in `ws.py`. The JSONL replay is trimmed to exclude the
in-progress logical turn so the two sources never overlap.

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

2. **Buffer replay is internal to `events()`** (see item 3) — a new subscriber's `events()`
   atomically snapshots `_turn_buffer` and registers its live queue, yielding the buffered
   in-progress turn first, then live events. A coalesced text block is a single `TextDelta`
   with the full accumulated text; after the client's `replayReset` the reducer builds the
   block from that one delta and subsequent live deltas append to it. Doing the snapshot
   *inside* `events()` (no `await` between snapshot and register) is what makes it race-free:
   a separate "emit the buffer, then subscribe" seam would drop any delta emitted in the gap.
   **`turn_active -> bool`** — a public property (`bool(self._turn_active)`) that `ws.py` reads
   to decide `drop_incomplete_turn`. `RemoteDriver` exposes the same attribute from the
   `ensure_driver` snapshot.

3. **Broadcast fan-out** — replace the single destructive `_out` queue with per-subscriber
   fan-out:
   - `self._subscribers: set[asyncio.Queue]`.
   - `_emit(e)` (replacing the ~19 `await self._out.put(e)` call sites): appends `e` to
     `_turn_buffer` when `e` is transcript content, then `put_nowait`s `e` to every
     subscriber queue. Non-blocking; unbounded queues (parity with today's `out_q`).
   - `events()` **synchronously** (no `await` between these two lines) snapshots
     `list(self._turn_buffer)` and adds a fresh queue to `_subscribers`; yields the snapshot,
     then drains the queue until `_STREAM_DONE`; unregisters the queue in `finally`.
   - `aclose()` broadcasts the `_STREAM_DONE` sentinel to every subscriber queue.
   - Two simultaneous in-process viewers now each receive the full stream, each with its own
     buffer snapshot.

   *Transcript-content classification:* only events that reconstruct the turn are buffered
   (`UserMessage`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`, `TurnEnd`,
   turn-scoped `ErrorEvent`). Control/ephemeral events (`Status`, `QueueUpdate`,
   `ContextUsage`, `Suggestions`, `Title`, `PermissionRequest`, `Question`, `Condensing`,
   `TurnFailure`) are **not** buffered — they are re-emitted from authoritative
   orchestrator/registry state by the existing `_emit_live_extras` path on attach, so
   buffering them would double-emit.

### Layer 2 — remote/daemon

4. **Subscribe from turn start, not from now.** In `daemon.py` `publish`, track two
   per-stream values derived from the published wire events:
   - `turn_start_seq[stream]` — set to the current `seq` when `event["type"] == "user"`.
   - `turn_open[stream]` — `True` on a `user` event; `False` on a `turnEnd` event **or** a
     `status` event whose `state` is terminal (`done`/`complete`/`needs_attention`/`idle`).
     Belt-and-suspenders so an error turn that emits no `turnEnd` still closes on its status.

   `ensure_driver` returns `resume_seq = turn_start_seq[stream] - 1 if turn_open[stream] else
   head_seq(stream)`, plus `turn_active = turn_open[stream]`. `RemoteDriver.events()` subscribes
   `since=resume_seq`; when a turn is open this replays the whole in-progress turn from the ring,
   otherwise it is from-now (the JSONL already has everything).
   - Bump the default replay-ring size (`CCSWITCH_IPC_REPLAY_BUFFER`) so a normal turn fits.
   - Overflow (a turn longer than the ring) falls back to the existing `gap_frame` →
     edge-resync-from-disk path. That is the pre-existing worst case, now narrowed to only
     turns that exceed the ring rather than every reconnect.

### Trim the in-progress turn from the JSONL replay

`replay_window(path, ..., drop_incomplete_turn: bool)` gains a flag. When true, it drops the
trailing in-progress logical turn: everything from the **last real `UserMessage`** (the same
"real user" test replay.py already applies — string/text content, has text or images, not
`isMeta`) to the end of the file. `ws.py` passes `drop_incomplete_turn=driver.turn_active`.
When no turn is active the flag is false and replay is unchanged.

### Uniform seam (`ws.py`)

`_handle_attach` reads `active = driver.turn_active` once, replays the JSONL with
`drop_incomplete_turn=active`, sends `replayComplete`, runs `_emit_live_extras`, then starts
the live stream. The in-progress turn is delivered **by `events()` itself** (not a separate
emit), so `ws.py` needs no new emit call — only the `turn_active`-driven JSONL trim.

- **In-process:** `events()` yields the buffer snapshot (the whole in-progress turn) then live;
  the trimmed JSONL stops before that turn. JSONL = prior turns, buffer = in-progress turn,
  queue = future. All disjoint.
- **Remote:** `RemoteDriver.turn_active` comes from the `ensure_driver` snapshot; its `events()`
  subscribes `since=resume_seq`, so the in-progress turn replays from the daemon ring while the
  trimmed JSONL stops before it. One source of truth per mode; the seam stays mode-agnostic.

## Why this avoids duplication

Two disjointness rules eliminate every overlap:

1. **JSONL vs in-progress turn:** the trimmed replay stops at the in-progress turn's user
   line, so the JSONL contributes only *prior* turns; the in-progress turn comes solely from
   the buffer (in-process) or the ring (remote). Verified safe by the spike: the user line is
   on disk before streaming, so the trim boundary is unambiguous.
2. **In-progress turn vs live future:** in-process, `events()` takes a synchronous buffer
   snapshot and registers its queue with no `await` between, so the snapshot is the past and
   the queue is strictly the future (no overlap, nothing missed). Remote, the single
   `since=resume_seq` subscription serves both replay and live from one ordered source. Keeping
   remote to that single source (the daemon ring, not a second driver-buffer emit) avoids the
   distributed-snapshot overlap (driver buffer vs `head_seq`).

## Testing

- **Driver unit tests:**
  - A late `events()` subscriber (opened mid-turn) receives the coalesced in-progress content
    as its first events, then subsequent live deltas exactly once (no dup, no missed gap).
  - Two concurrent subscribers each receive the full stream (broadcast).
  - `_turn_buffer` is cleared at turn settle → a subscriber opened while idle replays nothing
    (no dup against JSONL); `turn_active` is False.
  - The buffer coalesces consecutive same-block text into one `TextDelta` carrying the full
    accumulated text, in block order, interleaved correctly with tool calls/results.
- **Replay trim test:** `replay_window(path, drop_incomplete_turn=True)` drops everything from
  the last real `UserMessage` onward; with `False` (or no active turn) replay is unchanged;
  tool-result "user" lines are not treated as the trim boundary.
- **Daemon test:** `publish` sets `turn_start_seq` on a `user` event and flips `turn_open` on
  `user` / `turnEnd` / terminal `status`; `ensure_driver` returns `resume_seq=turn_start_seq-1`
  while a turn is open and `head_seq` once it closes; a subscriber replays the whole turn.
- **Integration:** simulate attach → stream partial turn → reattach; assert the reattached
  client sees the full message, not the tail.

## Known limitations

- **Turn-boundary settle window:** `_turn_buffer` is cleared and `turn_active` flips false at
  settle. If the SDK has not yet flushed the just-completed turn's final block to the JSONL at
  that exact instant, a coincident attach could miss that final block (the trimmed replay no
  longer trims it, but the disk copy may lag by <1s). Self-heals on the next attach/refresh.
  The spike shows the user line and completed blocks are flushed promptly, so this window is
  sub-second and far narrower than today's total in-progress-turn loss — a strict improvement.
- **Attach-time trim/settle TOCTOU:** `ws.py` reads `driver.turn_active` once, then does a
  threaded disk read (`replay_window`) + RPC re-emits before `events()` snapshots the buffer. If
  the turn *settles* in that window, the JSONL is trimmed (dropping the now-complete turn) while
  `_turn_buffer` has already been cleared → that turn is briefly absent on THIS attach. It
  self-heals on the next attach (`turn_active` is then False → full, untrimmed replay). The window
  is wider than a single flush (it spans the disk read), but bounded and self-healing; narrowing it
  further (snapshotting the buffer and the trim decision atomically) is deferred.
- **Remote long-turn overflow:** a single turn longer than the (bumped) replay ring falls back
  to disk resync, which lacks the in-progress turn. Bounded by ring size; acceptable for v1.

## Out of scope

- Persisting in-progress turns to disk (would remove the flush-race window entirely but is a
  larger change to the SDK-owned transcript path).
- Reworking the frontend reducer beyond what `live_turn_events()`'s existing wire shapes need.
