# Error Delivery to the UI — Design

**Date:** 2026-06-22
**Status:** Approved
**Goal:** Guarantee that every agent response — including error results and error codes —
reaches the browser UI. No abnormal outcome may be logged-and-swallowed.

## Problem

Output flows from the `claude` SDK subprocess to the browser through several hops:

```
SDK message → Translator.feed() → ev.* → SessionDriver._out queue
            → ws _live() forwarder → batcher → frontend reducer → React
```

Each hop can drop an error. Four swallow points were found, in priority order:

1. **`translator.py` `ResultMessage` handler** — the SDK's `ResultMessage` carries
   `is_error`, `subtype` (`error_max_turns`, `error_during_execution`), `result`,
   `errors`, and `api_error_status` (the literal HTTP code 429/500/529 of a failing
   API call). The translator discards all of it and emits a plain `TurnEnd`. An
   error result is indistinguishable from success — the user gets silence.
2. **`driver.py` `_drain()` except** — catches the exception but forwards a generic
   `ErrorEvent("driver_error", "session stream failed")`; the real type/message is
   only logged.
3. **`ws.py` `_live()`** — if `driver.events()` raises, it is `log.exception`'d and
   **nothing** is sent to the client.
4. **`reducer.ts` `case 'error'`** — `state.error` is a single overwriting slot, not
   part of the transcript timeline; a later error erases an earlier one.

## Principle

Every abnormal outcome becomes a first-class `error` wire event that the frontend
renders **inline in the transcript** (anchored to the failing turn) **and** surfaces
in the **banner**.

## Wire protocol

Extend the existing `error` event with an optional `turnId`. No new event type — one
error vocabulary, already redacted by `redact_event`, already routed by the WS bridge.

- `turnId` present → frontend appends an inline error block to that turn **and** sets
  the banner.
- `turnId` absent (protocol/attach errors, as today) → banner only.

This also fixes swallow point #4: the inline copy lives in the immutable transcript
timeline, so a later error cannot erase an earlier one — only the single banner slot
is overwritten.

`ErrorEvent` gains an optional `turn_id` (Python) / `turnId` (wire/TS), defaulting to
`None`/absent so existing emit sites are unchanged.

## Backend changes

1. **`translator.py` — `ResultMessage` (core fix).** Keep emitting `TurnEnd`
   (usage/cost preserved). *Additionally*, when `is_error` or `api_error_status` is
   set, emit `ErrorEvent(turn_id=…)`. Field → stable `code` map:
   - `subtype == "error_max_turns"` → `max_turns`
   - `subtype == "error_during_execution"` → `execution_error`
   - `is_error` and `api_error_status` set → `api_<status>` (e.g. `api_429`)
   - else → `agent_error`
   - `message` ← `result` text, else joined `errors`, else a per-`subtype` default.

   Successful results emit nothing new (their text already streamed via deltas).
   The error event is emitted before `TurnEnd`.

2. **`driver.py` `_drain()` except.** Replace the generic message with
   `type(exc).__name__` + a truncated `str(exc)`, anchored to the active turn id.

3. **`ws.py` `_live()`.** On exception, enqueue an `error` frame via
   `out_q.put_nowait(...)` before the task exits, so the user sees "live stream
   interrupted" instead of silence.

## Frontend changes

4. **`events.ts`** — add optional `turnId?: string | null` to `EvError`.
5. **`reducer.ts`** — new `Block` kind `'error'`; `case 'error'` keeps setting the
   banner and, when `turnId` is present, appends an error block to that turn.
6. **Transcript renderer** — render the error block inline (red, reusing the banner's
   styling).

## Testing (TDD)

- `translator`: each error `subtype` + `api_error_status` → correct `code`; success →
  no error event; `TurnEnd` still emitted.
- `driver`: `_drain` exception → `ErrorEvent` carrying the exception detail.
- `ws`: `_live` exception → error frame enqueued.
- `reducer`: error-with-`turnId` → inline block **and** banner; error-without-`turnId`
  → banner only.

## Out of scope (YAGNI)

No new event type, no severity levels, no retry UI, no separate error-history panel —
the transcript timeline is the history.
