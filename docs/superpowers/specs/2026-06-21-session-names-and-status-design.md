# Auto Session Names + Rich Status Indicators — Design

**Date:** 2026-06-21
**Status:** Approved (brainstorm) → pending implementation plan
**Author:** ccswitch

## Problem

The sessions dashboard lists every session by `name`, but `name` is whatever the
user typed at create time — and it defaults to the literal string `"New Session"`.
Sessions are therefore hard to tell apart at a glance. Two gaps:

1. **Names.** Claude itself generates a short, human-readable title for every
   session (the title shown in `claude --resume`). We don't surface it. The driver
   *does* emit a transient `title` event derived from `prompt.splitlines()[0][:50]`,
   but it is never persisted and never shown on the dashboard list — only inside the
   live session view's reducer state.
2. **Status.** Sessions show one of four coarse states (`working / idle / limited /
   ended`). The system already produces richer signals — a pending
   `permissionRequest`, a pending `question`, an `ExitPlanMode` (plan) request — but
   none of them reach the dashboard. The user cannot see which sessions are *waiting
   for them*, which are *ready for review*, and which are *done*.

This design surfaces an **auto-generated name** (when the user hasn't set one) and a
**richer, derived status** on every session row.

## Key enabling facts (verified against the codebase / Claude binary)

- Claude writes a structured JSONL transcript per session at
  `<config-dir>/projects/<encoded-cwd>/<session-id>.jsonl`, and records an
  **`ai-title`** (Claude-generated) and **`customTitle`** (user-set) for the session.
  This is the same title Claude shows in `claude --resume`. (Documented in
  `2026-06-20-rich-session-rendering-design.md`; `projects/` is symlinked into every
  account config dir, so the transcript is reachable regardless of rotation.)
- The repo already locates transcripts by **globbing the unique session id** across
  project dirs (commits for #15). We reuse that, not a hand-computed path.
- `SessionDriver` already emits `ev.Title(title, source)` and `ev.Status(state,
  count)`, already handles `PermissionRequest`, `Question`, and `ExitPlanMode`
  (`plan=True`), and already drives turn lifecycle (`_start_turn` → `_drain` →
  idle). The richer status is a **fold over signals that already exist**.
- DB `Session` row has `name` (required) and an unused nullable `title` column.
- Frontend already has `StatusPill` with a `SESSION_STATUS_MAP` and the needed
  lucide icons imported; `SessionCard` already renders `<StatusPill>` + `name`.

## Decisions (from brainstorm)

1. **Name source:** **reuse Claude's own generated title** (`ai-title` from the
   transcript), *not* a separate summarization LLM call and *not* only the
   first-prompt heuristic. Zero added token cost; consistent with what Claude shows
   natively. *(Constitution Principle III — verify against the real binary; Principle
   I — never fork Claude's behavior.)*
2. **Manual names always win.** A session name the user set manually is never
   overwritten by an auto-name. We add a `name_is_auto` boolean to distinguish the
   `"New Session"` default (auto) from a real user name (manual).
3. **Name resolution order:** user-set name → Claude `ai-title` → cleaned
   first-prompt heuristic (current behavior, improved) → `"New Session"`.
4. **Status is derived, not stored as ground truth.** A single derivation function
   folds the live signals into one dashboard status. The result is broadcast via the
   existing `status` event (extended) and persisted on the `Session` row so the list
   reflects it for detached/rehydrated sessions too.
5. **Status precedence (highest first):** `ended` > `limited` > `ready_review`
   (pending plan approval) > `waiting_input` (pending permission OR question) >
   `in_progress` (turn active) > `done` (≥1 turn completed, nothing pending/queued) >
   `idle` (created, never ran). A pending prompt-for-the-user outranks "working":
   if a turn is technically active but blocked on a permission, the session is
   `waiting_input` because that is the action the human must take.

## Status taxonomy

| Status          | Derived from                                              | Pill        |
|-----------------|----------------------------------------------------------|-------------|
| `in_progress`   | turn active (`status state=working`), nothing pending    | Zap, emerald|
| `waiting_input` | a `permissionRequest` or `question` is pending           | CircleAlert, amber |
| `ready_review`  | pending permission is `ExitPlanMode` (`plan=True`)       | Eye, blue   |
| `done`          | `turnEnd` seen, ≥1 turn ran, nothing pending or queued   | CheckCircle2, emerald |
| `idle`          | created, no turns yet                                    | Clock, gray |
| `limited`       | account rate-limited (existing)                          | AlertTriangle, amber |
| `ended`         | session closed (existing)                                | X, gray     |

`done` vs `idle`: `idle` = never ran; `done` = a turn finished and the session is
awaiting the next human prompt. This gives the dashboard a meaningful "finished its
work, your move" signal distinct from "freshly created, hasn't started."

## Architecture

### Backend

**Name resolution.**
- DB migration: add `Session.name_is_auto: bool` (default `True`). Manual rename
  (`PATCH /api/sessions/{id}`) sets it `False`.
- A small **title-reader** helper reads `ai-title` from the session transcript JSONL
  (reuse the existing session-id globbing). Returns `None` if absent/unreadable —
  never raises into the session loop.
- `SessionOrchestrator` resolves and persists the name: on turn end (and on
  rehydrate), if `name_is_auto` is `True` and a Claude `ai-title` is available, update
  `Session.name`. The driver's existing first-prompt `Title` event remains the
  interim name until Claude produces an `ai-title`.
- `_record_to_dict` already returns `name`; it additionally returns `name_is_auto`
  so the client can show an "auto" affordance if desired.

**Status derivation (the Learning-Mode contribution).**
- A pure function `derive_status(signals) -> str` in a new
  `server/sessions/status.py`, taking a small struct of booleans/counts
  (turn_active, pending_permission, pending_question, pending_plan, turns_completed,
  queue_depth, limited, ended) and returning one status string per the precedence
  rule above. Pure and table-tested.
- The driver/orchestrator calls it wherever it currently emits `ev.Status`, and on
  permission/question request+resolve, broadcasting the richer state and persisting
  it to `Session.status`.
- The `Status` wire event keeps `state` + `count`; `state` now carries the richer
  vocabulary.

### Frontend

- `SessionStatus` type gains `in_progress | waiting_input | ready_review | done`
  (keeping `idle | limited | ended`). Map server's snake_case at the API boundary.
- `SESSION_STATUS_MAP` in `UsageBadge.tsx` gains the four new entries (icons:
  `Zap`, `CircleAlert`, `Eye`, `CheckCircle2` — add `Eye` import). Colors are
  supplementary; the text label carries the meaning (existing a11y pattern).
- The live-session `reducer.ts` `status` case already stores `{state, count}`; no
  shape change, just richer `state` values flowing through.
- `SessionCard` already renders `<StatusPill status={session.status} />` and
  `session.name` — no structural change; it inherits the richer values.

## Testing (TDD)

- `server/sessions/status.py`: exhaustive table test of `derive_status` — every
  precedence pair (e.g. plan+permission → `ready_review`; working+permission →
  `waiting_input`; turnEnd+empty queue → `done`).
- Title-reader: fixture transcript JSONL with/without `ai-title`, malformed lines,
  missing file → `None`, never raises.
- Orchestrator: `name_is_auto` lifecycle — auto-name applied only when flag set;
  manual rename flips the flag and pins the name.
- Frontend: extend `reducer.test.ts` for new `state` values; `StatusPill` renders a
  label + icon for each new status (no crash on unknown — existing fallback).

## Out of scope (YAGNI)

- No new summarization model / API call.
- No live re-summarization as the conversation grows beyond what Claude already does.
- No user-configurable status colors or custom status names.
- No historical status timeline; only the current status per session.
