# Session status tiers: Done vs Complete vs Needs attention

**Date:** 2026-06-21
**Status:** Approved (brainstorming) — pending implementation plan

## Problem

The sessions list frequently shows a session as **Done** when it is not actually
finished — it may have errored, hit the model turn limit, had its stream die
mid-work (e.g. while waiting on a sub-agent), or simply ended a turn and be idle
awaiting the user. All of these collapse into a single green "Done", which reads
as "the work is finished" when it often is not.

### Root cause

`server/sessions/driver.py::_drain()` consumes the SDK turn stream and, in its
`finally` block, **unconditionally** sets `turn_active = False;
turns_completed += 1`. `server/sessions/status.py::derive_status()` then maps
`turns_completed >= 1` (with no active turn and nothing pending) to `"done"`.

That `finally` runs regardless of *how* the turn ended:

- a clean `ResultMessage(subtype="success")`,
- a `ResultMessage(subtype="error_max_turns" | "error_during_execution")`,
- or an exception in the drain (the `except` path that emits `ErrorEvent`).

The SDK's `ResultMessage` carries `subtype` / `is_error`, but the translator
discards them (it emits a bare `TurnEnd`). So the outcome of a turn is thrown
away, and every turn-end looks identical to the dashboard.

## Goals

1. Distinguish three terminal states for a session that has run at least one turn:
   - **Complete** — the agent signalled the session's objective is accomplished.
   - **Done** — the turn ended cleanly; the agent is idle / awaiting the user, but
     the objective was not declared finished.
   - **Needs attention** — the turn errored, hit the turn limit, or the stream
     died mid-work.
2. Make the feature self-contained: no user action and no manual prompting
   required for a session to reach **Complete**.

### Non-goals (explicitly out of scope)

- **Time-based stall detection** (a turn "active" but silent for N minutes). We
  chose outcome-based detection only — deterministic, no false positives.
- **Completion-language NLP** ("I've finished the task"). We use an explicit,
  collision-proof marker instead.
- **Detecting an implicit plain-text question** ("…which do you prefer?") as a
  waiting state. The existing `AskUserQuestion` → `waiting_input` path is
  unchanged; free-text questions still resolve to `Done`.

## Design

### Status vocabulary

A turn ending resolves to one of three terminal states (the existing
`idle` / `in_progress` / `waiting_input` / `ready_review` precedence is unchanged
and still wins over all of these):

| State              | When                                                                 | Badge                                    |
|--------------------|----------------------------------------------------------------------|------------------------------------------|
| **Complete**       | turn ended cleanly AND the completion marker is in the final message | green, `CheckCircle2`                    |
| **Done**           | turn ended cleanly, no marker                                        | **neutral slate/gray**, `Check`          |
| **Needs attention**| turn errored / hit turn limit / stream died mid-work                 | amber, `AlertTriangle`                   |

**Done is recolored** from its current emerald-green to neutral. Today "Done"
*looks* finished (green ✓); reserving green for **Complete** makes the color carry
the meaning — green = "the agent said it's actually done", everything else is
visibly provisional.

### Completion marker

A dedicated, collision-proof marker, defined as a single module-level constant:

```
<!-- ccswitch:complete -->
```

It is an HTML comment so it is invisible in the rendered transcript but trivially
parseable. The literal lives in **one** shared constant imported by both the
injector and the detector, so the two can never drift.

### Components

#### 1. Translator owns the SDK → outcome mapping (`server/sessions/translator.py`)

The translator already processes every `AssistantMessage` (text blocks) and the
final `ResultMessage`. Change:

- Accumulate the current turn's assistant text as it streams.
- On the `ResultMessage`, stamp the emitted `TurnEnd` event with an `outcome`:
  - `subtype != "success"` (or `is_error` truthy) → `"failed"`
  - else marker present in the turn's accumulated assistant text → `"complete"`
  - else → `"success"`
- Reset the accumulated text at turn start (the translator already has a
  `_reset()` on `ResultMessage`).

`ev.TurnEnd` gains an `outcome: str` field (default `"success"` for backward
compatibility / replay).

This keeps all SDK-message knowledge in the one module that already imports
`ResultMessage`; the driver stays SDK-agnostic.

#### 2. Driver tracks the last-turn outcome (`server/sessions/driver.py`)

- Add `self._last_turn_outcome: str = "success"`.
- In `_drain`, on the success path, read the `outcome` off the `TurnEnd` event(s)
  as they pass through and store the latest.
- In the `except` block (stream broke / crashed mid-turn — includes a hung
  sub-agent whose stream died) force `self._last_turn_outcome = "failed"`.
- The existing `finally` still flips `turn_active=False; turns_completed+=1` and
  emits the dashboard status, which now reflects the outcome.
- `interrupt()` (user Stop) sets the outcome to `"failed"`? **No** — a user-
  initiated stop is not "needs attention". `interrupt()` leaves the session
  `idle` as today (it sets `turn_active=False` and emits `idle` directly without
  incrementing `turns_completed`), so it is unaffected by this change.

#### 3. `derive_status` gains one signal (`server/sessions/status.py`)

`StatusSignals` gets `last_turn_outcome: str` (default `"success"`). The terminal
branch becomes:

```python
if sig.turns_completed >= 1:
    if sig.last_turn_outcome == "failed":
        return "needs_attention"
    if sig.last_turn_outcome == "complete":
        return "complete"
    return "done"
```

Precedence above this (pending plan → `ready_review`; pending permission/question
→ `waiting_input`; active turn → `in_progress`) is unchanged. Latest-turn-wins:
sending a new prompt after **Complete** moves the session back to `in_progress`,
and the next turn re-resolves.

`_dash_signals()` in the driver is extended to pass `last_turn_outcome`.

#### 4. Auto-inject the marker instruction (`server/sessions/sdk_factory.py`)

`real_sdk_factory` sets `system_prompt` to the Claude Code preset with an appended
instruction (verified supported on `claude-agent-sdk 0.2.105`):

```python
system_prompt={
    "type": "preset",
    "preset": "claude_code",
    "append": COMPLETION_INSTRUCTION,
}
```

where `COMPLETION_INSTRUCTION` is, in spirit:

> When you have fully accomplished the objective of this session, end your final
> message with `<!-- ccswitch:complete -->` on its own line. Emit it **only** when
> the work is genuinely complete — never for an interim step, and never while you
> are still working or waiting on something.

Appending preserves the default Claude Code system prompt (tools, CLAUDE.md via
`setting_sources`, etc.) — we only add to it. This makes **Complete** reachable
for every ccswitch session with **no user action**.

A new module `server/sessions/completion.py` holds both `COMPLETION_MARKER`
(the `<!-- ccswitch:complete -->` literal) and `COMPLETION_INSTRUCTION`, imported
by both `sdk_factory` (emitter) and `translator` (detector) — single source of
truth so the two can never drift.

#### 5. Frontend (`web/src/api/types.ts`, `web/src/components/UsageBadge.tsx`)

- Add `complete` and `needs_attention` to the `SessionStatus` union.
- Add entries to `SESSION_STATUS_MAP`:
  - `complete`: label "Complete", `CheckCircle2`, emerald (`text-emerald-700
    dark:text-emerald-400`).
  - `needs_attention`: label "Needs attention", `AlertTriangle`, amber
    (`text-amber-700 dark:text-amber-400`).
- Recolor `done`: label stays "Done", icon → `Check`, color → neutral
  (`text-slate-600 dark:text-slate-400` / `text-gray-600 dark:text-gray-400` to
  match the existing `idle` neutral).

## Data flow

```
SDK ResultMessage(subtype) ─┐
final assistant text ───────┴─▶ translator: TurnEnd(outcome ∈ {success,complete,failed})
                                        │
                                        ▼
                       driver._drain: self._last_turn_outcome = outcome
                       (except path  → "failed")
                                        │  finally: turn_active=False; turns_completed++
                                        ▼
                       driver._dash_signals() → StatusSignals(last_turn_outcome=…)
                                        │
                                        ▼
                       derive_status() → "complete" | "done" | "needs_attention"
                                        │  on_status callback
                                        ▼
                       orchestrator persists SessionRecord.status → list API → UsageBadge
```

## Error handling

- A failing `on_status` callback must never break the session loop (already
  guarded in `_emit_dashboard_status`); unchanged.
- The marker scan is a plain substring check on already-collected text — no
  parsing that can throw.
- `ev.TurnEnd.outcome` defaults to `"success"`, so replayed/old events and any
  code path that constructs a `TurnEnd` without an outcome behave as today (Done).

## Testing

- **`status.py`** (table tests): `last_turn_outcome` of `failed` → `needs_attention`;
  `complete` → `complete`; `success` → `done`; and that `pending_plan`,
  `pending_permission`, `pending_question`, and `turn_active` each still outrank
  the terminal branch.
- **`translator.py`**: `ResultMessage(subtype="success")` with the marker in the
  turn's assistant text → `TurnEnd(outcome="complete")`; without the marker →
  `outcome="success"`; `ResultMessage(subtype="error_during_execution")` →
  `outcome="failed"`.
- **`driver.py`**: a drain whose stream raises → `_last_turn_outcome == "failed"`
  and the emitted dashboard status resolves to `needs_attention`; a normal
  success turn → `done`; a success turn whose final text carries the marker →
  `complete`.
- **`sdk_factory.py`**: the built options carry a `system_prompt` preset whose
  `append` contains the marker literal (guards against the injector/detector
  drifting).
- **Frontend** (`UsageBadge` test): renders the correct label/icon for
  `complete` and `needs_attention`, and `done` uses the neutral class.

## Rollout

Pure additive behavior change; no schema migration (status is a free-form string
column, default `idle`). Ships through the existing auto-deploy. Existing
sessions re-resolve status on their next status emission; already-persisted
`done` rows simply stay `done` until the session next runs a turn.
