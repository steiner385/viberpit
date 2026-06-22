# Inline ghost-text next-prompt suggestion

**Date:** 2026-06-22
**Status:** Approved (brainstorm) → planning
**Supersedes the UX of:** PR #109 "context-aware next-prompt suggestion chips"

## Problem

PR #109 shipped next-prompt suggestions as a row of clickable chips above the prompt
bar. Two issues:

1. **It doesn't appear in practice.** Suggestion generation fires in exactly one
   place — `LocalDriver.drain()`'s turn-completion `finally` (`server/sessions/driver.py:347`).
   A session that is *resumed into an idle state* (the common case after a deploy
   restart) never hits that path, so `latestSuggestions` stays `[]` and nothing
   renders. Generation is also unobservable: `SuggestionService.generate` logs
   **only** on exception, so an empty/garbled model reply parses to `[]` silently.
2. **The chip UX is unwanted.** The desired interaction is a single faded
   "ghost text" suggestion shown inside the prompt box, accepted with **Tab** or
   **→**, not a row of chips.

## Goals

- Replace the chip row with a single inline ghost-text suggestion.
- Accepting (Tab / →) **fills the input** so the user can edit, then send manually
  — it does **not** auto-send.
- A tiny `⇥ Tab` hint makes the affordance discoverable.
- Suggestions actually generate for idle/resumed sessions, and generation is
  observable in logs.

## Non-goals

- No mid-typing autocomplete/completion. Ghost text shows only when the input is
  **empty**; it is a whole next-prompt, not a completion of partial text.
- No change to how suggestions are generated (model, prompt, isolation). We reuse
  `SuggestionService` and `real_suggestion_factory` as-is and just consume the top
  item.

## Reused pipeline (unchanged)

```
SuggestionService.generate(cwd, config_dir, context)   # Haiku one-shot, resume-less, tools denied
  → ev.Suggestions(session_id, turn_id, items)         # server event
  → WS → reducer case 'suggestions' → state.latestSuggestions
  → SessionView passes suggestions={rt.state.latestSuggestions} to Composer
```

The frontend keeps consuming `latestSuggestions: string[]`; ghost text uses
`latestSuggestions[0]`. The reducer already clears `latestSuggestions` on new-turn
events, so ghost text disappears when a turn starts.

## Frontend design (`web/src/components/session/Composer.tsx`)

- Compute `ghost = (!isWorking && text === '' && suggestions?.[0]) || null`.
- **Remove** the entire `data-testid="suggestion-chips"` block.
- Render ghost text as an absolutely-positioned overlay over the textarea's text
  origin: `pointer-events-none`, faded (same muted color as the placeholder), with a
  right-aligned `⇥ Tab` hint. Only rendered when `ghost` is non-null. Because it
  only shows on an empty box, no transparent-mirror text measurement is needed —
  the overlay sits at the textarea's padding origin.
- The native `placeholder` ("Send a message…") is suppressed while `ghost` is shown
  (they'd overlap); falls back to the normal placeholder when there's no suggestion.
- Key handling in `handleKeyDown`, only when `ghost` is non-null:
  - **Tab** (no Shift) → `preventDefault()`; `prefill(ghost)`. Shift+Tab is left
    alone (it's a SessionView navigation shortcut).
  - **ArrowRight** → `preventDefault()`; `prefill(ghost)`. (On an empty box the
    caret is trivially at the end, so this never fights cursor movement.)
  - `prefill` already focuses the textarea and grows its height.
- Typing anything makes `text !== ''`, which hides both the ghost overlay and hint.

## Backend design

### Trigger fix (`server/sessions/`)

Generation must also run when a session is **idle on load/resume**, not only at a
fresh turn boundary. Add a one-shot kick when a driver is created/resumed into an
idle state with prior conversation context, reusing `_maybe_start_suggestions()` so
the existing gates (`pending_permission`, `pending_question`, empty context) and the
fire-and-forget cancellation still apply. It must run at most once per idle entry
(guard against repeated attaches re-triggering generation). Exact call site
(driver construction vs. orchestrator resume path) to be pinned in the plan after
reading the resume/attach code; the behavior is: *idle session with context ⇒
exactly one generation attempt.*

### Observability

`SuggestionService.generate` (or `_run_suggestions`) emits an `INFO` log with the
outcome — number of items produced, or that the result was empty — so a black-box
"is it generating?" question becomes answerable from `journalctl -u ccswitch`.

## Testing

### Frontend — `Composer.test.tsx`
- Ghost text renders when idle + empty + `suggestions[0]` present.
- Hidden when working, when text has been typed, and when no suggestion exists.
- **Tab** fills the box with the suggestion and does **not** submit.
- **ArrowRight** fills the box and does **not** submit.
- **Shift+Tab** does **not** accept (no fill, default behavior).
- The `⇥ Tab` hint shows with the ghost text and disappears once text is typed.
- Remove the old chip-rendering / chip-click tests.

### Backend
- An idle, resumed session with prior context triggers exactly one generation
  attempt (the new path), gated off when a permission/question is pending.
- Generation logs its outcome (asserted via caplog or equivalent).
- Existing `server/tests/test_driver_suggestions.py` / `test_suggestions.py` updated
  for the new trigger; generation internals unchanged.

## Risks / verification

- The generator's real output has never been confirmed (0 log lines historically).
  During implementation, verify a real one-shot against an account config dir
  returns parseable suggestions before relying on the UI. The new INFO log makes
  this checkable on the live instance.
- Tab interception must not regress other Tab uses in the composer; scope the
  intercept strictly to "ghost is visible".
