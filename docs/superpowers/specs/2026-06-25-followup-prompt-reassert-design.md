# Follow-up prompts vanishing from the transcript — client re-assert fix

**Date:** 2026-06-25
**Status:** Design approved, pending spec review
**Scope:** Frontend only (`web/src/session/`). No server or wire-protocol change.

## Problem

A user's own **follow-up** prompts intermittently disappear from the session
transcript even though they go through (the model responds) and are eventually
written to the JSONL transcript. Observed symptoms: the prompt shows live as it's
typed, then is sometimes absent from the transcript; sometimes it never reappears
in the current view; navigating away and back recovers it.

### Root cause

A race between the wipe-and-rebuild on (re)attach and the SDK's transcript-file write:

1. **Two unreconciled IDs.** The live echo `UserMessage` carries a fresh random
   UUID (`driver.py:411`, `uuid.uuid4().hex`); the JSONL stores the same prompt
   under a different SDK-assigned UUID. Nothing matches the two.
2. **Blind wipe on every attach.** The client re-sends `attach` on mount AND on
   every WebSocket reconnect (`ws/client.ts:104-110`). The server answers every
   attach with a `replayReset` event that clears all turns (`reducer.ts:536-544`,
   `turns:[]`) and then replays the JSONL (`bridge/ws.py:200-202`). The wipe is
   deliberate — it prevents reconnects from duplicating append-only streamed deltas
   (`bridge/ws.py:192-199`).
3. **Replay reads disk at attach time.** `_replay_and_complete` reads the JSONL at
   that instant (`bridge/ws.py:399`) and only re-subscribes to the live stream
   afterward (`bridge/ws.py:401`). A reconnect landing **after** the live echo but
   **before** the SDK flushes the user message to JSONL means: `replayReset` wipes
   the live turn, the replay doesn't contain the prompt, and the live echo already
   fired on the old socket and is never re-emitted → the prompt is gone until the
   next attach.

**Why follow-ups specifically:** the *initial* prompt is explicitly protected
against "racing the transcript-file write" by being deferred to the first attach
(`bridge/ws.py:346-352`). Follow-ups have no such protection.

**Why "increasingly":** trigger frequency = attach frequency = reconnect frequency.
The app is mobile-accessible, and mobile WebSockets reconnect often (network
changes, backgrounding, screen sleep).

### Ruled out (with evidence)

- **Replay `isMeta`/empty-text filter** (`replay.py:78`): a scan of 100k+ transcript
  files / 25k meta entries found every drop to be a genuine skill body, slash-command
  body, or hook notice. ccswitch prompts are stored as plain-string `isMeta=null`
  and are always kept.
- **Queue dequeue path** (`driver.py:525-530`): correctly re-echoes; and the bug is
  reproduced with idle-state follow-ups anyway.
- **Replay windowing**: the default window includes trailing user messages correctly.

## Approach

**Client-side re-assert** (chosen over server reconcile and a replayReset rewrite for
being the smallest, lowest-risk change that cannot reintroduce the duplication bug).

Maintain a small "sent but not yet confirmed-on-disk" buffer in `TranscriptState`.
The reducer distinguishes a *live echo* from a *disk replay* via `state.replaying`
(true between `replayReset` and `replayComplete`). **Only a disk-replay sighting
confirms durability; a live echo does not.**

### Lifecycle of one follow-up

1. User sends it → dispatch `_trackSentPrompt {text}` → buffer gains an entry.
2. Live echo `user` event (`replaying === false`) → turn renders; buffer unchanged.
3. Reconnect → `replayReset` wipes turns; buffer survives (separate state).
4. Replay `user` events (`replaying === true`): each matching a buffered entry by
   text means that prompt is on disk → remove it from the buffer. This consume step
   is the **matching mechanism** — after a replay finishes, every entry left in the
   buffer is provably not on disk.
5. `replayComplete` → if this completion followed a `replayReset` (`hadReset`),
   append a synthetic user turn for **every** entry still in the buffer. No per-turn
   presence check is needed: the reset wiped `turns`, the replay rebuilt them from
   disk only, and disk turns never carry a `pending:N` id, so an unconditional append
   cannot duplicate.
6. Next reconnect: disk has caught up → step 4 removes the entry → step 5 appends
   nothing. Self-healed.

### Why the re-assert is gated on `hadReset` (as-built)

> **Implementation note.** An earlier draft of this spec guarded step 5 on "text not
> already present in `turns`". That guard is wrong: in the FIFO-duplicate case (two
> identical prompts, one already on disk) the on-disk turn for the *consumed* entry
> would falsely suppress re-assertion of the still-unconfirmed duplicate. The shipped
> code uses a `hadReset` flag instead, described below. See `reducer.ts` (`replayReset`
> / `replayComplete`).

The server sends `replayReset` only when the transcript is non-empty
(`bridge/ws.py:191`), but `replayComplete` **always** fires (`bridge/ws.py:205`). So
an attach against a not-yet-written transcript (and every error path) produces a
`replayComplete` with **no preceding wipe** — the live-echo turn is still present, and
re-asserting would duplicate it. The reducer sets `hadReset = true` in the
`replayReset` case and clears it in `replayComplete`; step 5 runs only when `hadReset`
is set. A bare `replayComplete` (no reset) therefore never re-asserts. Because a real
reset always wipes `turns` first and the consume step (4) already removed every
disk-confirmed prompt from the buffer, the remaining entries are exactly the
genuinely-unconfirmed ones — so step 5 appends them all, unconditionally and safely.

## Data structures

```ts
// added to TranscriptState
pendingSent: { id: string; text: string }[]   // FIFO, sent-but-unconfirmed-on-disk
pendingSentSeq: number                         // monotonic counter for stable ids
hadReset: boolean                              // true after replayReset; gates re-assert on replayComplete
```

- **Matching:** FIFO by text. A replay `user` event consumes the *first* buffered
  entry with equal text — so sending `"yes"` twice keeps both protected until each
  appears on disk independently.
- **Synthetic turn id:** `pending:<seq>`, distinct from live and JSONL ids, rendered
  via the existing `userTurnBlocks` helper for identical presentation.
- **Bounded:** cap at the last 20 entries (drop oldest). An entry can only linger if
  no further replay ever happens; the cap makes that harmless, and any on-disk entry
  is matched away on the next replay.

## Changes

**`web/src/session/reducer.ts`** (as-built)
- New `TranscriptState` fields `pendingSent` / `pendingSentSeq` / `hadReset`
  (init `[]` / `0` / `false`).
- New local action `_trackSentPrompt {text}`: push `{id:'pending:'+seq, text}`, bump
  seq, enforce the cap.
- `user` case: when `state.replaying`, remove the first `pendingSent` entry whose
  text equals the event text (the durability-match / consume step).
- `replayReset` case: set `hadReset: true`; leave `pendingSent` / `pendingSentSeq`
  untouched (the wipe must not drop unconfirmed prompts).
- `replayComplete` case: clear `hadReset: false`; if it *was* set, append a synthetic
  user turn for **every** remaining `pendingSent` entry (no per-turn presence check —
  see the `hadReset` rationale above).
- `LocalAction` union (lives in `reducer.ts`, not `events.ts`): add
  `{ type: '_trackSentPrompt'; text: string }`.

**`web/src/session/useSessionRuntime.ts`**
- In `onPrompt`, the normal-follow-up branch (the `prompt(...)` path), dispatch
  `_trackSentPrompt {text}` at send time. The armed-question branch is excluded (it
  already echoes its own answer bubble through a separate path).

## Scope boundaries (deliberate)

- **Initial prompt** unchanged — already protected server-side; optimistic-seed and
  adoption logic stay as-is.
- **Image attachments** — buffer holds text only. A re-asserted prompt that carried
  attachments shows its text immediately; images reappear when disk replay catches
  up. Known limitation, acceptable for a stop-the-bleeding fix.
- **Queued prompts** (sent while busy) are covered for free, since tracking happens
  at send time.
- No server or wire-protocol changes.

## Testing (vitest, reducer suite)

- `_trackSentPrompt` adds entries and enforces the cap.
- A replay `user` event (`replaying=true`) with matching text consumes one entry; a
  live echo (`replaying=false`) does not.
- **Race repro:** track `"hello"` → `replayReset` → `replayComplete` with no matching
  replay event → exactly one synthetic `"hello"` turn present.
- **Heal:** the next cycle with a matching replay `user` event → exactly one
  `"hello"`, buffer empty (no duplicate).
- **FIFO duplicates:** track `"yes"`,`"yes"` → one replay `"yes"` → one entry remains
  → one re-asserted.
- **No-wipe attach guard:** track `"hello"`, live echo present (turns has `"hello"`),
  `replayComplete` fires with no `replayReset` → no duplicate `"hello"` appended.

## Risks

- A re-asserted prompt whose text exactly equals an unrelated *earlier* on-disk prompt
  could in principle be consumed by the wrong disk sighting. FIFO-by-text consumption
  bounds this to consecutive identical texts and always errs toward *showing* a prompt,
  never hiding one — strictly safer than the current silent-drop behavior.
- Purely additive client state; cannot reintroduce the duplication bug the
  `replayReset` wipe was built to fix (the wipe stays intact).
