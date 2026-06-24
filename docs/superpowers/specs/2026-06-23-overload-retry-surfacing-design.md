# Overload-retry surfacing — honest 5xx/529 handling for live turns

**Date:** 2026-06-23
**Status:** design — pending review
**Area:** `server/sessions/` (failure, driver, translator), `web/src/session/` (status tier, transcript error)
**Extends:** `2026-06-22-failure-driven-account-rotation-design.md` (the `failure.classify` source of truth),
`2026-06-22-error-delivery-to-ui-design.md` (the `error` wire event carrying `api_error_status`),
`2026-06-23-session-resilience-hardening-design.md` Gap 4 (the stall watchdog)

## Problem (observed in prod, 2026-06-23)

Anthropic returned a burst of transient **`529 Overloaded`** responses (`overloaded_error`,
real `request_id`s) between ~14:47–15:01 UTC. The SDK handled them correctly — exponential
backoff, `maxRetries: 20`. But ccswitch turned a *recoverable* storm into *dead* sessions:

1. **The stall watchdog interrupted turns mid-backoff.** `driver._note_progress` advances the
   watchdog clock only on non-empty `TextDelta`/`ThinkingDelta` or `ToolCall`/`ToolResult`/
   `TurnEnd`/`Question`/`PermissionRequest`. A turn sitting in 529 backoff emits only
   `system`/`api_error` events — *none* of which count — so after `STALL_TIMEOUT` (300s) the
   watchdog classified the actively-retrying turn as "wedged" and `interrupt()`ed it, killing
   the recovering retry. (It also looks frozen to the user, who then hits stop.)
2. **The failure was mislabeled.** When the SDK finally gave up, `failure.classify` mapped the
   5xx/529 result to `kind="other"` → the UI showed a generic red **"Turn failed"** (and the
   raw SDK blob "API Error: 500 Internal server error"). Nothing distinguished a transient
   overload from a real, terminal failure, so the user re-sent `continue` straight back into the
   same trap.

Net effect: five sessions stuck `needs_attention`, "rejecting prompts", while status.claude.com
stayed green (529 is load-shedding, not an incident).

## Goal

Make a transient upstream overload **visibly transient and self-healing**, never a dead session:
- the watchdog must not abort a turn that is actively retrying an API error;
- the UI must say "Anthropic overloaded — retrying" (amber), not "Turn failed" (red), while
  retries are in flight; and
- if the SDK exhausts its retries, the terminal message must name the cause and tell the user to
  re-prompt — not pretend it was a generic crash.

## Non-goals

- **No new retry logic.** The SDK owns turn-level retry/backoff (`maxRetries: 20`); ccswitch only
  stops *fighting* it. (`oauth.http_request` backoff — `2026-06-22-api-retry-backoff` — is a
  *different* path: usage-poll / token-refresh, not the in-session turn stream. Untouched here.)
- **No account rotation on overload.** Every account hits the same overloaded backend; rotating
  wastes the rotation cap and the new account 529s too. Overload is explicitly *not* recoverable-
  by-rotation.
- No config UI, no persistence (overload is transient by nature), no change to the watchdog's
  core purpose (catching genuinely wedged, silent turns).

## What already exists (reuse, don't rebuild)

- `failure.classify(code, message, *, retry_after)` returns a `TurnFailure(kind=…)` with
  `_RECOVERABLE = {"rate_limit", "auth_401", "process_crash"}`. We **add one kind** alongside.
- `translator.py` already reads `ResultMessage.api_error_status` and emits a first-class `error`
  event (per `error-delivery-to-ui`). We extend the code it emits.
- `driver._note_progress(e)` is the single chokepoint for the watchdog clock. One added branch.
- `web/src/session/statusTier.ts` is the single source of truth for status label/tier/icon. One
  added tier; no new status map.

## Two moments, three changes

A 529 storm touches two distinct moments. The design handles both and routes them to one honest
UI.

### Moment A — *during* the turn (SDK is retrying)

**Change 1 — watchdog counts retries as progress** (`driver.py:_note_progress`).
Add `api_error` (the streamed `system`/`api_error` event, surfaced as `ev.ErrorEvent` with a
transient code — see Change 2) to the set of events that advance `_last_progress_at`. Rationale:
the stall watchdog (`session-resilience-hardening` Gap 4) exists to catch turns "making no
progress" — emitting only *empty* deltas with the subprocess idle. A turn receiving 529 retry
responses is the opposite of idle: the SDK is alive and the server is answering. This refines
Gap 4's "meaningful event" definition to include "actively recovering from a transient API
error." A genuinely wedged turn emits no `api_error`s, so it is still caught; and the SDK's
`maxRetries: 20` bounds the deferral — once exhausted the turn ends and Moment B applies.
`STALL_CAP` (3 interrupts / 120s) remains the runaway backstop.

**Change 3a — transient UI state** (`statusTier.ts` + transcript).
While a turn is active and its most recent error event is a transient overload, derive a new
**`retrying`** tier → amber label **"Overloaded — retrying (N/20)"** (attempt count from the
`api_error` event when present, else just "Overloaded — retrying"). This replaces the red
"Turn failed" *for an in-flight turn*, so neither the user nor reflex says "it died."

### Moment B — *at* turn failure (SDK gave up)

**Change 2 — classify overload** (`failure.py` + `translator.py`).
- `translator.py`: when `ResultMessage.api_error_status ∈ {500, 502, 503, 529}` (or the message
  carries `overloaded_error` / "Overloaded"), emit error code `api_overloaded` (next to the
  existing `api_429` / `api_401`). The streamed mid-turn `api_error` events get the same code so
  Change 1/3a can recognize them.
- `failure.classify`: `code == "api_overloaded"` → `TurnFailure(kind="overloaded")`. Content
  fallback: a `5xx`/"overloaded" message with no auth/429 signal → `overloaded`. **Not** added to
  `_RECOVERABLE` (no rotation, no auto-respawn). It is a *labeling* kind: the orchestrator keeps
  today's "surface + end turn" behavior, but the surfaced error is now specific.

**Change 3b — honest terminal message** (`statusTier.ts` + transcript error text).
A turn that *ends* on `overloaded` shows a terminal but non-alarming message:
**"Anthropic overloaded — retries exhausted. Re-send when capacity recovers."** Still
`needs_attention` (the user must act) but with overload-specific copy, not the raw
"API Error: 500 Internal server error" blob.

## Data flow

```
SDK 529  ──stream──▶ translator (api_error_status=529 → ErrorEvent code=api_overloaded)
                         │
       during turn ──────┼──▶ driver._note_progress(ErrorEvent api_overloaded) → advance clock  [Change 1]
                         │                                          └─ watchdog leaves turn alone
                         └──▶ ws → reducer → statusTier 'retrying' → amber "Overloaded — retrying" [Change 3a]

SDK exhausts 20 retries ▶ ResultMessage(is_error, api_error_status=529)
                         ▶ translator → ErrorEvent code=api_overloaded
                         ▶ failure.classify → kind="overloaded" (not recoverable)  [Change 2]
                         ▶ statusTier → "Overloaded — retries exhausted, re-send"   [Change 3b]
```

## Files touched

- `server/sessions/translator.py` — emit `api_overloaded` from `api_error_status` 5xx/529 and
  `overloaded_error` messages (mid-turn `api_error` events and the final `ResultMessage`).
- `server/sessions/failure.py` — `overloaded` kind in `classify` (+ content fallback); **not** in
  `_RECOVERABLE`.
- `server/sessions/driver.py` — `_note_progress` advances the clock on an `api_overloaded`
  `ErrorEvent`.
- `web/src/session/statusTier.ts` — `retrying` tier (amber) + overload-specific `needs_attention`
  copy; single source of truth, no parallel map.
- Transcript/error rendering — overload error event renders the specific copy, not the raw blob.
- Tests: `server/tests/` (translator code, classify, watchdog-leaves-retrying-turn-alone with an
  injectable clock + fake driver emitting only `api_overloaded`); `web/src/**/__tests__` (status
  tier label/tier for `retrying` and exhausted-overload).

## Test plan (TDD, failing-first)

1. **`classify`**: `api_overloaded` code → `kind="overloaded"`, `is_recoverable is False`; 529/503
   message fallback → `overloaded`; a 401/429 message is unaffected (regression guard).
2. **translator**: `ResultMessage(api_error_status=529)` and an `overloaded_error` stream event
   both yield an `ErrorEvent(code="api_overloaded")`.
3. **watchdog**: a fake driver emitting only `api_overloaded` events within the window is **not**
   interrupted (clock advanced); a driver emitting only empty `thinkingDelta`s **is** interrupted
   after `STALL_TIMEOUT` (the Gap-4 regression guard still holds).
4. **statusTier**: active turn + latest event `api_overloaded` → `retrying` tier, amber, label
   contains "Overloaded"; ended overload turn → `needs_attention` with exhausted copy.
5. **Both paths.** Logic lives in the in-process driver/orchestrator the daemon runs; a remote-mode
   assertion confirms the `error` event reaches the edge unchanged.

## Delivery

One feature, sequenced as: backend classification + translator (Changes 1–2, pure/TDD) → watchdog
progress branch (Change 1 wiring) → frontend tier + copy (Changes 3a/3b). Backend and frontend may
land as two PRs or one; each is independently green. Per CLAUDE.md: non-draft PR, auto-merge armed,
required checks (`lint-and-test`, `frontend`) green.
