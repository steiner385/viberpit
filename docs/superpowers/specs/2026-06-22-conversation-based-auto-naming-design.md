# Conversation-based auto-naming — design

**Date:** 2026-06-22
**Status:** Approved (pre-implementation)
**Feature area:** session auto-naming (`server/sessions/`)

## Problem

Auto-naming effectively names sessions from the **opening prompt**, which carries too
little signal early in a conversation to produce an accurate name. We want the name to
reflect the **entire conversation** (prompts *and* assistant turns).

## Current behaviour (verified against the code and real transcripts)

Two title mechanisms exist today and they drift apart:

1. **First-prompt slug** — `driver.py:213-218`: on the *first* prompt only, the first 50
   chars of the first line are emitted as `Title(source="prompt")` **to the live
   WebSocket stream only** (`driver._out`). It updates the in-session header
   (`web/src/session/reducer.ts`) but is **never written to the DB `name`**.

2. **Claude's transcript title** — `orchestrator.py:_maybe_apply_auto_name`: on every
   `status=="done"`, reads Claude's own `aiTitle`/`summary` from the transcript JSONL
   (`titles.py`) and writes it to **DB `name` + the in-memory record only**. It already
   re-runs each turn and adopts the latest title ("last non-empty wins"), guarded by
   `name_is_auto` (manual-rename pin) and a `title == rec.name` idempotency check.

**Consequences:**
- The **dashboard card** (`record.name`) already shows Claude's conversation-derived
  title — but the **live in-session header stays stuck on the first-prompt slug forever**,
  because nothing emits a new `Title` event when Claude's title is adopted.
- Claude writes `aiTitle` **asynchronously**, with highly variable timing — observed in
  real transcripts as early as line 7 and as late as line 299 of 309. Because we only
  re-read on `status=="done"`, a title Claude writes *after* the final turn (or slowly
  mid-turn) is missed until the *next* turn completes — which may never happen.

Claude's `aiTitle` is genuinely conversation-derived and good quality (it reads prompt +
response), so leaning on it is the right signal; the gaps are **propagation** and
**cadence**.

## Goals

- Make Claude's transcript title the single source of truth for the auto name.
- Surface it on **both** the dashboard card and the live in-session header.
- Keep it **fresh as the conversation grows**, including titles Claude writes after the
  last turn.

## Non-goals

- No self-summarisation / extra LLM call (zero added cost — option explicitly rejected).
- No filesystem watcher per session (rejected for complexity).
- No change to manual-rename behaviour.

## Design

### 1. `_maybe_apply_auto_name` — trigger-agnostic + propagates to the live header

- Decouple the method from the `"done"` call site so it can be called from two places
  via one idempotent code path. **The `"done"` call in `_persist_status` is untouched.**
- When it adopts a new title, in addition to updating `rec.name` + DB `name`, push
  `ev.Title(title=..., source="claude")` into `self._drivers[session_id]._out` so the
  in-session header converges with the dashboard.
- Existing guards retained: skip when `not name_is_auto`; skip when `title == rec.name`
  (idempotent — no redundant DB writes or header events).
- If there is no live driver (detached session), update DB/record but skip the `_out`
  push (`self._drivers.get(session_id)` guard).

### 2. Periodic titling loop (added, alongside the kept turn-boundary trigger)

- A background `asyncio` task started/stopped next to the existing `_rotation_task` and
  `_poller` in `start()` / `stop()`.
- Every ~15s it iterates `self._records` and, for each session that is **active
  (`status != "ended"`) and still `name_is_auto`**, calls the refactored
  `_maybe_apply_auto_name`.
- It keeps re-reading even after a title exists (cost is one small JSONL read per active
  auto-named session), so the name tracks a conversation that shifts topic by adopting
  Claude's latest title.
- The interval is a constructor arg (sibling to `poll_interval`) for testability.

### 3. First-prompt slug demoted to placeholder

No logic change to `driver.py:213`. It still emits the instant `Title(source="prompt")`
for immediate feedback; it simply stops being the durable name. The moment Claude's title
exists, the `source="claude"` event supersedes it in the header (the DB name was already
Claude-sourced).

## Triggers (both retained)

- **Turn boundary (kept):** `_persist_status` calls `_maybe_apply_auto_name` on every
  `status=="done"` — immediate re-read when a turn completes.
- **Light periodic (added):** the ~15s loop calls the same method for active, still-auto
  sessions — the safety net for titles written after the final turn or slowly mid-turn.

## Data flow

1. First prompt → `Title(source="prompt")` slug shows instantly in the header.
2. Claude writes `aiTitle` into the transcript JSONL (async, variable timing).
3. Either trigger fires `_maybe_apply_auto_name`: if `name_is_auto` and the title differs,
   update `rec.name` + DB `name` (dashboard) **and** push `Title(source="claude")` into
   the driver's `_out` (live header).
4. Both surfaces show Claude's conversation-derived title; the periodic loop keeps it
   current.

## Edge cases

- **Manual rename** pins `name_is_auto=False` → both triggers skip forever (existing guard).
- **Idempotence**: skip when `title == rec.name` → no redundant DB writes or header events.
- **Detached/ended sessions**: loop filters `status != "ended"`; a detached session with
  no live driver still updates DB name but skips the `_out` push.
- **Title read never raises** (`titles.py` degrades to `None`) → the loop can never break
  the session manager.

## Testing (TDD)

- `_maybe_apply_auto_name` adopts a newer `aiTitle`: updates record + DB and enqueues a
  `Title(source="claude")` on the driver's `_out`.
- Respects `name_is_auto=False`: no change, no event.
- Idempotent when the title equals the current name: no event, no DB write.
- Periodic loop picks up a title written with **no further turn** (the core gap) and skips
  ended sessions.
- Frontend reducer: a `title` event with `source="claude"` updates the header (existing
  path; add a case/type assertion for the new `source` value).

## Affected files

- `server/sessions/orchestrator.py` — refactor `_maybe_apply_auto_name`; add periodic loop
  + start/stop wiring + interval arg.
- `server/sessions/events.py` — allow `source="claude"` on `Title` (type/validation).
- `web/src/session/events.ts` / `reducer.ts` — accept the new `source` value (display
  path already exists).
- Tests under `server/tests/` (and a `web/` reducer assertion).
