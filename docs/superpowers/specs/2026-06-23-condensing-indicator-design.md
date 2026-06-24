# Context-Condensing Indicator — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorming)
**Feature:** Surface when a live `claude` session is auto-compacting ("condensing") its
context, plus a threshold-aware context-usage readout — mirroring what Claude Code shows.

## Problem

ccswitch drives `claude` sessions through `claude-agent-sdk` and surfaces their state in the
dashboard and the in-session view. Today there is **no indicator** for context compaction: when a
session pauses mid-turn to auto-compact, the UI just shows "Working…" with no hint that a condense
is happening, and there is no escalating warning as the context window fills.

## Goal & non-goals

**Goal:** reproduce what Claude Code actually surfaces:
1. A transient **"Condensing context…" status** (spinner + elapsed time) while compaction runs.
2. An **always-visible context readout** that **escalates color** as the window fills.

**Non-goal — a determinate progress bar / ETA.** Verified against the installed SDK
(`claude_agent_sdk` 0.2.105): compaction is a single opaque blocking model call. The protocol
exposes only discrete signals — a `PreCompact` start hook and a post-compaction `compact_boundary`
marker — never incremental progress. Claude Code itself shows an *indeterminate* spinner, not a
countdown. We match that; we do not invent an ETA that no signal can back.

## Verified SDK signals (Principle III)

| Signal | Fires | Delivery | Reaches ccswitch today |
|---|---|---|---|
| `PreCompact` hook (`types.py` `HookEvent`, `trigger: manual\|auto`) | **before** compaction | in-process async `HookCallback` registered via `ClaudeAgentOptions.hooks` | No — `sdk_factory` registers no hooks |
| `compact_boundary` SystemMessage (`compact_metadata: {trigger, pre_tokens}`) | **after** compaction | stream message via `receive_response()`; `message_parser.py:261` routes unknown system subtypes to a generic `SystemMessage` | Arrives, but `translator.py:52` drops every `SystemMessage` whose `subtype != "init"` |

Start = the in-process `PreCompact` hook (deterministic, fires immediately). End = the
`compact_boundary` stream message. Belt-and-suspenders: also clear on the next `TurnEnd`.

## Architecture (Approach 1: condensing as a first-class derived status)

The condensing state rides the **existing status pipeline** end-to-end; the meter **enhances an
existing readout**. No parallel implementations.

### Piece 1 — Condensing status (NEW; backend + frontend)

**Backend detection**
- `server/sessions/driver.py`
  - New in-memory flag `self._condensing: bool = False` (constructor).
  - New bound async callback `_on_pre_compact(input, tool_use_id, ctx) -> {}` — sets
    `self._condensing = True`, then `await self._emit_status()`. Returns an empty
    `HookJSONOutput` (never blocks compaction).
  - `_dash_signals()` passes `condensing=self._condensing`.
  - In `_drain`, on an `ev.Condensing(active=False)` event: set `self._condensing = False`
    (the event is internal — consumed, not forwarded). The turn-completion path in `_drain`'s
    `finally` also resets `self._condensing = False` so the flag can never outlive the turn.
- `server/sessions/translator.py`
  - Extend the `SystemMessage` branch: on `subtype == "compact_boundary"`, read
    `msg.data.get("compact_metadata")` and emit `ev.Condensing(active=False, trigger=..., pre_tokens=...)`.
- `server/sessions/events.py`
  - New internal event `Condensing(active: bool, trigger: str | None = None, pre_tokens: int | None = None)`.
    Internal-only (like `RateLimit`): **no `to_wire`** — the UI learns via the derived `status`.
- `server/sessions/status.py`
  - `StatusSignals` gains `condensing: bool = False`.
  - `derive_status` adds a rung **above `turn_active`** (compaction happens mid-turn, so it must
    outrank `in_progress`) and below the needs-you states:
    `if sig.condensing: return "condensing"` placed immediately before the `turn_active` check.
- `server/sessions/types.py`
  - Add `condensing` to the canonical status comment/list.

**Factory wiring (the one cross-cutting change)**
- `server/sessions/sdk_factory.py`: `real_sdk_factory(..., *, on_pre_compact=None)`; when provided,
  add `hooks={"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}` to `ClaudeAgentOptions`.
- `server/sessions/orchestrator.py`: the `_session_factory` wrapper
  `_factory(cwd, config_dir, can_use_tool, mode, resume)` gains `*, on_pre_compact=None` and
  forwards it to the base factory.
- `server/sessions/driver.py`: both factory call sites pass `on_pre_compact=self._on_pre_compact`.
- **Test factories must accept the kwarg.** Update the shared `fake_sdk_factory()` helper to accept
  and ignore `on_pre_compact`, and add `**_kw` to the handful of inline `def factory(cwd, config_dir,
  can_use_tool, mode, resume)` definitions in `server/tests/`. (Blast radius is mechanical; enumerated
  in the plan.)

**Frontend presentation**
- `web/src/session/statusTier.ts`: add a `condensing` entry to `PRESENTATION` —
  `{ tier: 'busy', label: 'Condensing…', Icon: Minimize2, textClass: BUSY }` (lucide `Minimize2`
  reads as "shrinking/compacting", distinct from the `Loader2` used for "Working…").
  This makes the **card** StatusPill render "Condensing…" automatically (status-driven, no card change).
- `web/src/components/session/StatusBar.tsx` + `WorkingIndicator.tsx`: the busy tier renders
  `WorkingIndicator`, which hardcodes the "Working…" phrase. Add an **optional `phrase`/`srLabel`
  override** prop to `WorkingIndicator` (mirroring the existing `subAgentCount` override pattern),
  and have `StatusBar` pass `phrase="Condensing context"` when `status.state === 'condensing'`. This
  reuses the spinner + live elapsed readout — no parallel spinner component.

### Piece 2 — Context readout color escalation (ENHANCEMENT; frontend only)

The readout already exists: `lib/contextWindow.ts#remainingContextPct(usage, model)` → `contextPct`
in `session/useSessionRuntime.ts:298` → rendered as `ctx {pct}%` in
`components/session/SessionMeta.tsx:114-120`. `contextPct` is **remaining** percent (100 = empty).

- Add a small pure helper (co-located in `lib/contextWindow.ts`) mapping remaining-% to a severity:
  e.g. `> 25` → neutral, `10–25` → warning, `< 10` → danger. Single source of truth for the
  thresholds; table-testable.
- `SessionMeta.tsx`: apply the severity → Tailwind color class on the existing `ctx %` span
  (reuse the `NEEDS_YOU`/`ERROR` palette already used elsewhere). When `< 10`, append a terse
  "· compacts soon" hint. No new component, no new state, **no backend change.**

## Data flow

```
PreCompact hook ─▶ driver._on_pre_compact ─▶ _condensing=True ─▶ _emit_status
                                                              └▶ Status{state:"condensing"} ─▶ WS ─▶ pill + StatusBar spinner
compact_boundary ─▶ translator ─▶ Condensing(active=False) ─▶ driver clears _condensing ─▶ _emit_status ─▶ back to in_progress
turnEnd.usage (already flowing) ─▶ reducer.usage ─▶ remainingContextPct ─▶ SessionMeta ctx% (now color-escalated)
```

## Error handling / edge cases

- **No stuck "Condensing…".** `_condensing` is in-memory only; never persisted to the DB `status`
  column. On server restart / WS reconnect / transcript replay there is no flag, so `derive_status`
  returns `in_progress` (or the real state). Turn-end reset is the backstop if `compact_boundary`
  never arrives.
- **Hook never fires but boundary does** (e.g. SDK behavior change): the `Condensing(active=False)`
  path is idempotent — clearing an already-clear flag is a no-op.
- **Unknown context window / no usage yet:** `remainingContextPct` already returns `null` → readout
  shows the existing `DASH`; severity helper returns neutral for `null`. Meter degrades gracefully.
- **`compact_metadata` shape:** read defensively (`.get`), tolerate missing fields; `pre_tokens` is
  informational only and never required to clear the flag.

## Testing

**Backend (pytest)**
- `status.py`: table-test `derive_status` — `condensing=True` outranks `turn_active`, yields
  `"condensing"`; needs-you states still outrank condensing.
- `translator.py`: new fake builder `sys_compact_boundary(trigger, pre_tokens)` →
  `feed()` emits `ev.Condensing(active=False, ...)`; a non-init/non-boundary system message still
  yields `[]`.
- `driver.py`: calling `_on_pre_compact` sets the flag and emits a `condensing` Status; a subsequent
  `Condensing(active=False)` (or turn end) clears it back to `in_progress`.
- factory: `real_sdk_factory(on_pre_compact=cb)` registers a `PreCompact` `HookMatcher` in options
  (assert via the constructed options / a thin seam).

**Frontend (vitest)**
- `statusTier.test`: `statusPresentation('condensing')` → busy tier, "Condensing…" label.
- `WorkingIndicator`: `phrase` override renders "Condensing context…" and keeps the elapsed readout.
- `contextWindow.test`: severity thresholds (neutral / warning / danger / null).
- `SessionMeta`: `ctx %` span gets the danger class and "compacts soon" hint at low remaining.

## Out of scope (possible follow-ups)

- True `autoCompactThreshold`-accurate meter (plumb `autoCompactThreshold`/`isAutoCompactEnabled`
  from the `init` message through `SessionMeta`). The model-window heuristic already in
  `contextWindow.ts` is good enough for v1; revisit if the heuristic proves inaccurate against real
  init payloads.
- Surfacing the post-compaction `pre_tokens`/"context reset" as a timeline marker.
