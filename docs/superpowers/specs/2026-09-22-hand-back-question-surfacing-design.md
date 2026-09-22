# Hand-back question surfacing — design

Status: proposed
Date: 2026-09-22

## Problem

A session that calls `hand_back(reason)` gets the exact same "Answer needed"
badge (`waiting_question` status, `statusTier.ts:73`) as a session that asked
a real structured question via `AskUserQuestion`. But nothing about *what* is
being asked is ever surfaced:

- `hand_back`'s `reason` **is already sent to the browser** — carried on the
  wire `turnEnd` event as `handBackReason` (`server/sessions/events.py:107`,
  emitted in `server/sessions/translator.py:342`) — but the frontend discards
  it. `EvTurnEnd` (`web/src/session/events.ts:27`) doesn't even declare the
  field, and the reducer's `turnEnd` case (`web/src/session/reducer.ts:1071`)
  only extracts `usage`/`costUsd`.
- The real question UI (`QuestionCard`/`PendingQuestionPanel`,
  `web/src/features/sessions/panels/`) never fires for a hand-back, because
  it only renders a *live* `question` block
  (`PendingQuestionPanel.tsx:19-28`). A live question is a **blocking future**
  the turn is paused on — answered via the WS `answerQuestion` control message
  (`web/src/ws/client.ts:330`), which resolves a server-side
  `QuestionRegistry` and has its own orphan/reattach semantics
  (`server/sessions/orphaned_asks.py`, `Question`/`QuestionResolved` in
  `events.py:220-248`). `hand_back` has already **ended** the turn — there is
  no future to resolve, so that machinery does not apply.

Net effect: the user sees a badge claiming their input is needed, plus
whatever prose the agent happened to write before calling the tool, with no
UI element that says "here is the question" — the screenshot that prompted
this spec.

## Goals

1. Always surface `hand_back`'s `reason` in a clearly-labeled UI element —
   fixes the reported bug on its own, no schema changes required.
2. Let an agent optionally attach a small set of discrete choices to a
   hand-back, rendered as clickable options, for the common case where the
   ask genuinely has a short list of answers ("keep A or B?").
3. Never invent a second live-future/resolution system. A clicked option is
   just a way to send the next chat message — nothing more.
4. Backward compatible: every existing `hand_back(reason)` call (tool-call
   and Codex marker paths) keeps working unchanged; `options` is additive.

## Non-goals

- Changing how `Question`/`AskUserQuestion` works.
- Making `hand_back` require options, or making the choice mid-turn/blocking.
- Multi-select or freeform-escape-hatch parity with `QuestionCard` — a
  hand-back reply is always free text via the normal composer; options are a
  shortcut for the common answers, not the only way to reply.
- Codex marker parity: `parse_hand_back_marker` stays reason-only. Codex
  lacks `durable_tool_surface` and keeps the HTML-comment marker path
  (core.md: "the marker parsers are permanent"); adding a structured-options
  marker grammar is out of scope and not requested.

## Approaches considered

1. **(Rejected) Require options on every hand_back**, mirroring
   `AskUserQuestion`. Breaks every existing free-text hand-back — many asks
   are genuinely open-ended ("the PR is blocked on X, tell me how you want to
   proceed") and don't reduce to a short enum. Would also require touching
   every engine's calling convention at once.
2. **(Rejected) Route hand-back options through the real `Question`/
   `QuestionRegistry` future system** (emit an `ev.Question` for it). Wrong
   semantics: `Question` exists because the turn is paused *inside* a
   blocking tool call, and needs reattach/orphan handling for exactly that
   reason (see the `Question`/`QuestionResolved` docstrings). A hand-back's
   turn is already over; on reload there is nothing to "reattach" to, the
   reason/options are just persisted turn content like any other block. Reusing
   the future machinery would add real complexity (orphan detection, a
   registry entry, reconcile-on-reconnect) to solve a problem that doesn't
   exist here.
3. **(Chosen) Two-tier, additive fix**: always carry+render `reason`;
   optionally carry+render `options`; clicking an option calls the existing
   "send a chat message" path (`onPrompt`), starting an ordinary new turn.
   No new resolution mechanism, no DB-tracked entity — `options` is transient
   per-turn display data, exactly as ephemeral as `reason` already is.

## Design

### Data shape

`options`, when present, reuses `propose_decision`'s existing shape (already
JSON-schema'd at `server/sessions/tools/tools.py:276-281`, already a familiar
convention for both agents and this codebase) rather than inventing a new
one:

```json
{"key": "keep-a", "label": "Keep approach A", "detail": "optional, one sentence"}
```

`key` and `label` required, `detail` optional, 2-6 items when present (same
bounds as `propose_decision`; fewer than 2 isn't a choice, more than 6 stops
being a quick pick). Unlike `propose_decision`, hand-back options are **not**
persisted as a DB-tracked decision/work-item entity — no work item is
required to call `hand_back` at all (it's the tool for a session the user
started themselves, per the existing `propose_decision` docstring at
tools.py:265). Validation is therefore a plain type/shape check in the tool
layer, not a service call.

### Backend

**`server/sessions/tools/tools.py`**
- Add a module-level `_CHOICE_OPTIONS_SCHEMA` (or similarly named) constant
  holding the `{key, label, detail}` items schema, and use it from both
  `hand_back`'s and `propose_decision`'s `spec_tool(...)` schema argument —
  today the shape is inlined twice in spirit (once here, would be twice more
  if `hand_back` inlined its own copy); one literal, two call sites.
- Extend `hand_back`'s schema: add optional `options` (array, the shared
  schema, `minItems: 2, maxItems: 6` where JSON-schema supports it).
- Extend `hand_back`'s tool description with the same one-line guidance
  `propose_decision` gives: when to include `options` ("a short, genuinely
  enumerable set of answers") vs. when to omit it (open-ended asks stay
  reason-only).

**`server/sessions/completion.py`**
- `parse_hand_back(args)`: after validating `reason`, if `args` has an
  `options` key, validate it inline (list, 2-6 items, each a dict with
  non-empty string `key`+`label`, optional string `detail`) and raise
  `ToolArgError` with a corrective message on failure — same pattern as
  `parse_mark_no_pr`. Return `{"reason": ..., "options": list | None}`.
- `parse_hand_back_marker(text)`: unchanged. Continues to return
  `{"reason": ...}` with no `options` key; call sites must treat a missing
  key the same as `None` (never assume it's present).

**`server/sessions/translator.py`** (~line 306-343)
- `hand_back = signals.get("hand_back") or parse_hand_back_marker(...)` — no
  change to this line.
- When building `ev.TurnEnd(...)`, also pass
  `hand_back_options=(hand_back.get("options") if outcome == "hand_back" and
  hand_back else None)`, mirroring the existing `hand_back_reason` line
  exactly.

**`server/sessions/events.py`**
- `TurnEnd`: add `hand_back_options: list[dict] | None = None`. Add
  `"handBackOptions": self.hand_back_options` to `to_wire()`.

No changes to `server/sessions/status.py` — `waiting_question` remains the
right status; the fix is about what renders underneath the badge, not the
badge itself.

### Frontend

**`web/src/session/events.ts`**
- Extend `EvTurnEnd` with the three fields already on the wire but untyped:
  `outcome?: string`, `handBackReason?: string | null`,
  `handBackOptions?: { key: string; label: string; detail?: string }[] | null`.
  (Scoped narrowly to what this fix needs — `prResolution`/`modelRequest`
  stay untyped/unused; wiring those up is a separate concern, not touched
  here.)

**`web/src/session/reducer.ts`**
- Add `handBack: { turnId: string; reason: string; options: Option[] | null }
  | null` to session state.
- `turnEnd` case: when `ev.outcome === 'hand_back'`, set `state.handBack` from
  `ev.handBackReason`/`ev.handBackOptions`. Clear it back to `null` in the
  existing `'user'` case (reducer.ts:568/715 — fires when the user's own
  prompt is echoed back, whether typed or sent via an option click) and in
  the existing `'turnStart'` case (reducer.ts:579/792 — the next turn
  beginning). Either one already fires before any new turn's content would
  need to coexist with the stale panel, so `handBack` never lingers past the
  turn it describes.

**`web/src/features/sessions/panels/HandBackPanel.tsx`** (new)
- Reads `state.handBack`; renders nothing when null.
- Same visual register as `PendingQuestionPanel` (`border-warning`,
  `bg-warning-wash/60`, ARIA `role="region" aria-live="polite"`) so it reads
  as "the same kind of thing" without literally being one.
- Body: the `reason` text, styled as the headline ask (not buried prose).
- If `options` present: render each as a plain button. `onClick` calls the
  existing prompt-send path with the option's `label` text — **not** a new
  `onAnswer`/WS message. This is the crux of avoiding a second resolution
  system: from the server's point of view, clicking an option is
  indistinguishable from the user typing that label and hitting send.
- No "chat instead" escape hatch needed (`QuestionCard`'s freeform toggle) —
  the normal composer below is always available and always free text; the
  buttons are a shortcut, not a mode.

**`web/src/features/sessions/SessionBody.tsx`**
- Mount `<HandBackPanel />` alongside `<PendingQuestionPanel />` (~line 240).
  They are mutually exclusive in practice (a turn ends in at most one
  terminal outcome) but are kept as separate components rather than merged,
  since their underlying semantics (ended turn vs. paused turn) genuinely
  differ and a merged component would need a branch for every difference
  anyway.

### Docs

- `docs/product/features/session-tools.md`: update the `hand_back`
  description to mention optional `options` and when to use them.

## Error handling

- Malformed `options` from the tool call (wrong shape, out-of-range count):
  `ToolArgError` sent back to the model as a corrective message, exactly like
  every other tool argument error in this file — no partial state, the turn
  is *not* marked as a hand-back, the model gets a chance to retry in the
  same turn.
- Missing/`None` `options` on the wire (existing callers, the marker path):
  frontend treats absent the same as `[]` — panel renders reason-only.
- A hand-back that arrives with `options` on a client that hasn't loaded the
  updated frontend build: unrecognized wire field is simply ignored by
  today's reducer (already the behavior for `handBackReason` right now) —
  no crash, just the pre-fix behavior for that one session view.

## Testing

Backend (`server/tests/`):
- `parse_hand_back`: reason-only (existing coverage, unaffected); valid
  options (2, 6 items) accepted; 1 item / 7 items / missing `label` / non-list
  each raise `ToolArgError` with a corrective message.
- Translator: a `hand_back` tool-use with `options` produces a `TurnEnd` with
  `hand_back_options` populated; a marker-path hand-back produces `None`.
- Tool schema: `hand_back`'s JSON schema round-trips through whatever test
  currently exercises `propose_decision`'s schema shape (reuse, don't
  duplicate, per the shared-constant point above).

Frontend (`web/src` vitest):
- Reducer: `turnEnd` with `outcome: 'hand_back'` sets `state.handBack`;
  a subsequent `turnStart`/prompt clears it.
- `HandBackPanel`: renders nothing when `handBack` is null; renders reason
  text when present with no options; renders option buttons when options
  present; clicking a button calls the same send-path spy a normal composer
  send would call (assert equivalence, not a new code path).

Manual/UI check (per this project's norm of exercising real UI changes): a
Claude Code session that calls `hand_back(reason="…", options=[...])`
against a dev server, confirming the panel appears, reason reads clearly, and
clicking an option sends it as the next message and starts a new turn.

## Open questions

None outstanding — resolved during design:
- *Does this reuse `QuestionCard`'s resolution wiring?* No (see Approaches
  §2/§3) — deliberately a new, simpler component.
- *Does Codex get structured options too?* No — marker path stays
  reason-only (Non-goals).
- *Is `options` persisted as a decision/work-item record like
  `propose_decision`?* No — purely transient per-turn display data; no work
  item is required to call `hand_back`.
