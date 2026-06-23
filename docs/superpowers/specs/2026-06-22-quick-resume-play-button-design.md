# Quick-Resume Play Button — Design

**Date:** 2026-06-22
**Status:** Approved (pending spec review)
**Feature area:** Web frontend (session list + composer) + session backend (on-demand suggestions)

## Problem

Sessions frequently stall — Claude stops mid-task, or finishes a turn and waits. Today the user
unsticks them by manually typing a nudge (`continue`, `keep going`, or a context-specific instruction)
into the composer and pressing Enter. The nudge content varies per session, so a fixed-string button
won't do. We want a **one-tap play button** that gets a stalled session moving again with minimal
friction.

## Key realization

The system **already generates** the right nudge. The suggested-next-prompts feature
(`server/sessions/suggestions.py`, specs `2026-06-22-suggested-next-prompts-design.md` and
`2026-06-22-inline-ghost-text-suggestion-design.md`) asks Claude for 2–4 short, context-aware next
prompts whenever a session goes idle, ships them in a `suggestions` event, and renders the first as
**ghost text** in the composer (`Composer.tsx:47`, accepted today via Tab/→ which only *fills* the box).

This feature makes those suggestions **directly actionable** — the play button *sends* the
auto-generated suggestion instead of requiring Tab-then-Enter. The "it varies per session" problem is
solved for free, because the suggestion is generated from that session's own context.

This feature does **not** introduce a new draft, a new generator, or a hardcoded nudge. It is a new
*affordance over existing data*.

## Core behavior

A play button (▶) **acts on the session's auto-generated suggestion**. It never touches user-typed
text — the normal Send button owns that case.

The governing rule ("C-rule"), keyed on whether a suggestion was already on screen:

| Situation | Behavior |
|---|---|
| A suggestion already exists | **Send it instantly** (user has effectively seen it; low surprise) |
| No suggestion exists yet | **Generate one on the fly**, then **fill-and-confirm** (never auto-send sight-unseen text) |
| User has typed their own text | Play button hidden — normal Send handles it |

Because every generated suggestion in the set is "already on screen-equivalent," **picking any item
from the multi-suggestion menu also sends instantly**. The generate-on-the-fly path applies only to the
*primary* play tap when **zero** suggestions exist.

## Components

### A. Prompt-line button — `web/src/components/session/Composer.tsx`

- **Placement:** beside the existing submit button; visible only in non-working states
  (idle / done / needs-attention / waiting). Hidden whenever the user has typed their own text
  (`text !== ''`), since the ghost/suggestion concept is already gated on `text === ''`.
- **Icon:** lucide `Play`; swaps to `Loader2` (spinner) while an on-demand generation is in flight.
  Reuses existing button styling.
- **Primary tap logic:**
  - If `latestSuggestions[0]` exists → `onPrompt(latestSuggestions[0])` immediately.
  - Else → send `request_suggestions`, set a local `pendingGenerate` flag. When the `suggestions`
    event arrives *and* `pendingGenerate` is set, call the existing `prefill()` (`Composer.tsx:121`)
    to drop it into the box focused, and clear the flag. User confirms with a second tap / Enter.
- **Multi-suggestion menu:** a caret affordance, shown only when `latestSuggestions.length >= 2`,
  reveals `latestSuggestions[1..]`. Picking any item sends it instantly (all are existing). Reuse an
  existing popover/dropdown primitive if the UI kit has one; provide a touch-friendly reveal
  (caret tap target, with long-press on the play button as an alternative).

### B. List-slot button — `web/src/components/SessionCard.tsx`

- **Placement:** alongside the existing Rename / Archive / Delete actions, same `actionBtn` style,
  lucide `Play` icon, same desktop hover-reveal / touch-always-visible behavior.
- **Primary tap logic (C-rule, adapted — there is no composer on a card):**
  - **Existing suggestion** → **fire in place, stay on the list** (sends the prompt to the session
    without navigating; supports batch-kicking several stalled sessions).
  - **None yet** → **open the session and run the prompt-line generate→fill-and-confirm path**
    (reuses component A's logic; the fresh suggestion is never auto-sent).
- **Multi-suggestion menu:** same as A — caret shown when `>= 2` suggestions exist; picking fires in
  place instantly.
- **Gating:** shown only for sessions in non-working states.

## Backend / plumbing (new, minimal)

1. **On-demand generation trigger.** New client→server WebSocket message
   `{type: 'request_suggestions', sessionId}`. The server runs the **existing** generation path in
   `server/sessions/suggestions.py` and emits the **normal** `suggestions` event. One generator, one
   event, two triggers (automatic idle-time + on-demand). No second way for the client to receive a
   draft.
   - On-demand generation may need to bypass some of the automatic-path gating (e.g. it should run
     even if automatic suggestions were skipped). Confirm the exact gate set during implementation;
     reuse the same generator function regardless.

2. **Surface suggestions in the session list payload.** Add the latest suggestion list to the
   `Session` API/model so `SessionCard` knows whether a suggestion exists and can send/menu it without
   mounting the session runtime. New field is **nullable** (dev SQLite `init_db` backfills nullable
   columns; a NOT NULL column would need a real migration).

3. **Send-from-list path.** A way to send a prompt to a session the dashboard does **not** currently
   hold a live WebSocket for. Reuse the **same server-side engine handler** the WS `prompt` message
   already invokes — do not create a parallel prompt path. (Likely a small REST endpoint, e.g.
   `POST /sessions/{id}/prompt`, that calls into the existing handler; exact transport decided in the
   plan.)

## Data flow

```
Existing-suggestion tap (either button):
  click → onPrompt(items[0]) / send-from-list(items[0]) → existing prompt handler → session resumes

No-suggestion tap (prompt-line):
  click → ws.send(request_suggestions) → suggestions.py generator → `suggestions` event
        → pendingGenerate set → prefill() into composer (focused) → user confirms → onPrompt

No-suggestion tap (card):
  click → open session → (prompt-line no-suggestion path above)

Menu pick (either button):
  click item[i] → onPrompt(items[i]) / send-from-list(items[i]) → existing prompt handler
```

## Error handling

- **Generation times out / returns empty:** clear `pendingGenerate`, restore the play icon, leave the
  composer empty and editable. No error modal — the user can type a nudge as they do today. (Match the
  existing best-effort, quiet-fail behavior of the suggestion feature.)
- **Send-from-list to a session that has ended / errored:** surface the same error path the in-session
  prompt would (reuse existing error-to-UI delivery); do not silently swallow.
- **Disconnected socket:** the existing `_send` returns false when the socket isn't open; the button
  should reflect that it didn't fire (no spinner-stuck state).

## Testing

- **Frontend (vitest):**
  - Composer: play visible only in non-working states and only when `text === ''`; hidden when typing.
  - Existing suggestion → tap sends `items[0]` immediately (asserts `onPrompt` called with it).
  - No suggestion → tap sends `request_suggestions`, shows spinner; on `suggestions` event, composer is
    prefilled and focused, **not** auto-sent.
  - Menu: caret present only when `>= 2` suggestions; picking item *i* sends `items[i]`.
  - SessionCard: existing suggestion → fires in place (send-from-list called, no navigation); none →
    navigates into the session.
- **Backend (pytest):**
  - `request_suggestions` invokes the existing generator and emits a `suggestions` event.
  - Session list payload includes the latest suggestions (nullable when none).
  - Send-from-list routes through the same engine handler as the WS `prompt` message.

## Out of scope

- Per-session **user** draft persistence (we act on auto-generated suggestions, not typed drafts).
- Hardcoded `continue` fallback / user-editable nudge lists.
- Auto-sending freshly generated suggestions sight-unseen (explicitly rejected by the C-rule).

## Open questions for implementation (not blocking design)

- Exact send-from-list transport (REST endpoint vs. transient WS) — pick the one that reuses the
  existing prompt handler most directly.
- Which automatic-path gates to bypass for on-demand generation.
- Whether the card's action row is too crowded on mobile with play + caret added; if so, collapse the
  caret into a long-press on the play button on touch.
