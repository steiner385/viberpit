# Session Freshness & Status Consolidation — Design

**Date:** 2026-07-02
**Status:** Draft (awaiting review)
**Author:** Claude (opus-4-8) with Tony

## Problem

Three UI symptoms are reported together and share one architectural root cause:

- **(a) Stale / lost transcripts** — live agent responses arrive delayed or vanish entirely.
- **(b) Wrong "active Xm ago"** — freshness indicators are incorrect and flicker.
- **(c) Wrong status groupings** — sessions land in the wrong dashboard status buckets.

Root cause: **multiple parallel, unreconciled sources of truth for freshness and status
that race and overwrite each other** — the exact "N parallel implementations" anti-pattern the
constitution (Principle VI) warns against. The backend already has a careful canonical model; the
frontend and the update pipeline quietly re-implement pieces of it and disagree.

## Investigation summary (confirmed root causes)

The backend intent (`server/sessions/status.py:27-40`) is sound:

- `last_turn_at` is the canonical "active … ago" clock; it advances **only** on `ACTIVITY_STATUSES`
  (`done`/`complete`/`needs_attention`/`waiting_input`/`waiting_question`/`ready_review`).
- `in_progress`, `condensing`, `waiting_external` deliberately do **not** reset the clock.
- `derive_status()` is the single, table-tested status derivation.

That intent is defeated downstream:

| Concept | Canonical source | Conflicting parallel implementation(s) | Verdict |
|---|---|---|---|
| Freshness ("active ago") | `last_turn_at` → REST `updatedAt` via `iso_utc` (`api/sessions.py:128`) | ① `web/src/ws/client.ts:367,376` fabricates `updatedAt = new Date().toISOString()` (client clock) on **every** WS `status`/`rotated` — for any status. ② `SessionCard.tsx:429` renders `relTime(updatedAt)` with **no local tick**, freezing between 5s polls. ③ Two separate `useNow` hooks (`hooks/useNow.ts` and a private one in `SubAgentList.tsx:19`). | **Real** |
| Status (dashboard groups) | `derive_status()` → serialized verbatim | Two update channels write the same React Query cache with **no reconciliation**: the 5s poll (`api/hooks.ts:66`) and WS `_patchSessionStatus`. An in-flight poll response (snapshot taken before a status change) can clobber a fresher WS status. The `statusTier`/`triageTier` *presentation* mapping is legitimate and stays. | **Real** (the race, not the mapping) |
| Transcript delivery | Live WS stream → `transcriptReducer`; JSONL replay on reconnect | Reducer has **no sequence numbers / gap detection**; a dropped event while the socket stays connected is silently missing with no self-heal. Live transcript has **no polling safety net** (WS-to-reducer only). Wire `batch` frame carries **no `sessionId`**, so the reducer cannot filter out events from a previously-attached session during a switch (`useSessionRuntime.ts:97` dispatches every event unconditionally). Healing exists only via reconnect-triggered `replayReset`. | **Real** (priority) |
| Account active/idle counts | `_ACTIVE_STATUSES` (`orchestrator.py:76`) | None. Feeds only `active_session_count`, `live_session_load`, `live_session_counts` → account rotation + Accounts panel. Does **not** drive session-dashboard groupings. | **False alarm — out of scope** |

### Key naming hazard

`Session.updated_at` (row-write column, bumped on **every** persist via `onupdate`, `models.py:99`)
and `Session.last_turn_at` (activity clock) are distinct but confusingly named, and get merged in
people's heads. This spec keeps them strictly separate and gives them **distinct wire names**.

## Design principle

**The client never *computes* freshness or status — it only *displays* server-provided values and
*detects when it is missing data*.** Every parallel implementation collapses into one authoritative
source per concept:

- Freshness = server `last_turn_at` (wire: `updatedAt`). Client renders it and ticks a clock; it
  never fabricates it.
- Status ordering = a server-provided monotonic token per session. Client keeps the newest; neither
  update channel can move a session backward.
- Transcript integrity = server-provided monotonic per-session event sequence. Client detects gaps
  and self-heals via the existing replay path.

## Delivery: one design, three staged PRs

Sequenced by user priority (transcripts first) and dependency. Each PR is independently reviewable,
shippable, and testable. The shared spec guarantees they consolidate rather than add new parallel
paths.

---

### PR 1 — Transcript sequence numbers + gap detection + auto-replay (priority)

**Goal:** make lost/dropped live events *detectable* and *self-healing*, and stop cross-session
contamination.

**Server (`server/sessions/driver.py`, `server/bridge/ws.py`):**

1. Assign a **monotonic per-session `seq`** to every event at its single emission point
   (`SessionDriver._emit`), so the number is a property of the event carried through the whole
   pipeline (buffer, subscriber queue, batcher). Assigning at the source means a silent drop
   *anywhere downstream* (e.g. a swallowed redaction exception in `_batcher`) produces a detectable
   gap.
2. The wire `batch` frame gains: `sessionId`, `firstSeq`, `lastSeq` (and per-event `seq` is
   available if needed). Direct (non-batched) session-event frames carry `seq` + `sessionId`.
3. `seq` is a live-stream property only. Replayed-from-JSONL events (which do not pass through
   `_emit`) do not carry meaningful live seqs; the client establishes its baseline from the **first
   live batch after `replayComplete`** (see client step 3).

**Client (`web/src/ws/client.ts`, `web/src/session/useSessionRuntime.ts`):**

1. **Session filter (defense in depth):** drop any `batch`/session-event whose `sessionId` is not the
   active session. This closes the switch-contamination window and prevents another session's stream
   from false-triggering gap detection.
2. **Gap detection:** track `lastAppliedSeq` for the active session. If an incoming batch's
   `firstSeq !== lastAppliedSeq + 1`, a gap occurred.
3. **Baseline:** after `replayComplete`, adopt the first live batch's `firstSeq` as the baseline
   (no gap fired for the first batch). Thereafter require contiguity.
4. **Self-heal:** on a detected gap, trigger a re-attach (`reattach()` already exists in
   `useSessionRuntime`), which emits `replayReset` → full JSONL replay → `replayComplete`, rebuilding
   the transcript cleanly. Debounce/rate-limit re-attach so a persistently gappy stream cannot storm.
5. Surface nothing to the user on a successful self-heal (silent recovery); log via `clientLogger`
   for observability.

**Out of scope for PR 1:** changing the batcher's 16 ms coalescing (benign) or the JSONL write path.

**Tests:**
- Reducer/gap unit: contiguous seqs apply; a gap dispatches a reattach request.
- `sessionId` filter: events for a non-active session are ignored.
- Baseline: first live batch after `replayComplete` never false-fires.
- Server: `_emit` assigns strictly monotonic per-session seqs across buffer + live subscribers.
- Integration: a simulated dropped event triggers exactly one replay and the transcript ends
  complete.

---

### PR 2 — Freshness single-source

**Goal:** kill the fabricated client-clock timestamp and the frozen/duplicated relative-time render.

**Server (`server/sessions/orchestrator.py::_persist_status`, WS status publish):**

1. The WS `status` message gains `updatedAt: string` — the **same** value the REST endpoint sends:
   `iso_utc(rec.last_turn_at or rec.created_at)`. Because `last_turn_at` only advances on
   `ACTIVITY_STATUSES`, a transition to `in_progress`/`condensing`/`waiting_external` carries the
   prior turn's time — the intended behavior, now honored end-to-end.

**Client (`web/src/ws/client.ts`, `web/src/hooks/useNow.ts`, `web/src/components/…`):**

1. `WsStatusMsg` gains `updatedAt`; `_patchSessionStatus(sessionId, status, updatedAt)` uses the
   server value verbatim. **Delete** `updatedAt: new Date().toISOString()`.
2. `_patchSessionAccount` (rotation) must **not** touch `updatedAt` at all — a rotation is not a fresh
   turn. It updates only `accountName`.
3. **Unify `useNow`:** one hook in `hooks/useNow.ts`, parameterized by tick condition
   (page-visible, plus optional "only while active"). `SubAgentList` consumes it; delete its private
   copy. `SessionCard` consumes it so "active Xm ago" advances smoothly between updates instead of
   freezing until the next poll.

**Tests:**
- WS status patch sets `updatedAt` from the server payload, never from `Date.now()`.
- Rotation patch leaves `updatedAt` unchanged.
- Single `useNow` drives both `SessionCard` and `SubAgentList`; no second timer hook remains
  (grep guard in test).

---

### PR 3 — Poll/WS reconciliation (version-guarded merge)

**Goal:** neither the 5s poll nor a WS patch can overwrite a session with older data. Poll remains a
safety net.

**Server:**

1. Expose a **monotonic per-session ordering token** on both the REST `/sessions` payload and the WS
   `status` message. Two candidate implementations, finalized in the plan:
   - **(a) Reuse the existing row-write `Session.updated_at`** (bumped on every persist) as wire
     `revAt` — zero new columns, already monotonic-ish per session.
   - **(b) A dedicated nullable `rev` integer** bumped on each status persist — collision-free,
     needs a nullable column (respects the "add columns nullable" dev-SQLite constraint).
   Recommendation: start with (a) `revAt`; escalate to (b) only if same-instant writes prove to
   collide in practice.

**Client (`web/src/api/hooks.ts`, `web/src/ws/client.ts`):**

1. Both the poll `queryFn` result and every WS patch merge into the `QK.sessions` cache by
   **"newest ordering-token wins, never overwrite a session field with data from an older token."**
   A stale in-flight poll response can no longer clobber a fresher WS status, and vice versa.
2. Grouping/ordering (`triageTier`/`ordering.ts`) is unchanged — it reads the reconciled
   `session.status`, which is now always the newest.

**Tests:**
- A poll response carrying an older token does not overwrite a WS-patched newer status.
- A WS patch older than the current cache entry is a no-op.
- Ordancy: after interleaved poll + WS updates, the cache reflects the highest-token record per
  session.

---

## Data flow (target state)

```
Freshness:  last_turn_at (server, ACTIVITY_STATUSES only)
              └─> REST updatedAt ─┐
              └─> WS status.updatedAt ─┴─> cache (version-guarded) ─> relTime + useNow tick
Status:     derive_status (server) ─> REST + WS status ─> cache (version-guarded) ─> triageTier groups
Transcript: driver._emit(seq) ─> batch{sessionId,firstSeq,lastSeq} ─> client gap-check
              gap? ─> reattach ─> replayReset ─> JSONL replay ─> replayComplete (baseline reset)
```

## Non-goals / out of scope

- `_ACTIVE_STATUSES` and account rotation/load logic (verified unrelated).
- The `statusTier`/`triageTier` presentation mapping (legitimate, not a duplicate).
- The 16 ms WS batch-coalescing window (benign).
- JSONL write/flush timing in the SDK (owned by claude-agent-sdk, per constitution Principle I).
- ReviewView's `useSessionTranscript` (`staleTime: Infinity`) — historical view, not a live-freshness
  concern; leave unless PR 1 testing surfaces an issue.

## Risks & mitigations

- **Reattach storms** on a persistently gappy stream → debounce/rate-limit self-heal in PR 1.
- **Seq baseline mismatch** with the in-progress `_turn_buffer` snapshot on attach → resolved by
  baselining off the first *live* batch after `replayComplete`, not off replayed events; covered by
  tests.
- **Same-instant `revAt` collisions** in PR 3 → fallback to a dedicated `rev` integer (option b).
- **Backend/RPC parity:** any new orchestrator-exposed value must round-trip through the
  RemoteOrchestrator proxy + registered RPC (see project memory: request-layer orchestrator methods
  need a daemon proxy), or breadth tests fail. The WS/status changes here ride existing publish
  paths, but the PR 3 token must be present in the RPC-serialized session record.

## Verification gates (per PR)

`pytest server/tests/` · `vitest run` · `tsc --noEmit` · `eslint` (mypy runs in CI — verify locally;
see project memory). Each PR opens non-draft with auto-merge armed once green (project completion
workflow).
