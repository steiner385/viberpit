# Pin Pending Question to the Composer — Design

**Date:** 2026-06-21
**Status:** Approved (design) — pending implementation plan

## Problem

When the agent asks an `AskUserQuestion`, the conversation **does** pause (the driver blocks the turn until answered), but the answer UI — the `QuestionCard` — renders **inline in the transcript**, where it scrolls out of view. The only bottom-of-screen affordance is a sticky banner that literally reads **"Action needed — scroll up to respond"** (`SessionBody.tsx`). So the user must hunt upward for the question instead of answering where their attention (and the composer) already are.

## Goal

Surface a **live, open** question in a **pinned answer panel directly above the composer**, so it's answered in place — no scrolling. Historical/replayed questions stay inline, read-only. Answering must **immediately send** the response to the agent and **immediately clear** the pinned panel / "action needed" affordance.

## Decisions (from brainstorming)

- **Pin only when answerable.** Pin the interactive panel only for a *live* question (one that arrived over the WS stream, with a real `questionId` the driver is blocking on). On detached/finished sessions the question came from replay (synthesized id, not answerable) → it stays **inline, read-only** (the existing replay rendering).
- **One at a time.** When several questions are open (a multi-question `AskUserQuestion`), pin the **first** open live question; the next pins automatically once the first is answered and removed.

## Approach: a `live` flag distinguishes answerable from historical

The clean discriminator between "answerable now" and "historical" is **how the question arrived**:
- **Live** — over the WS stream, after replay completed; the driver is blocking on its real `questionId`.
- **Replayed** — synthesized from the CLI JSONL during transcript replay (synthetic id, not answerable).

The reducer already tracks `state.replaying` (flipped false by the `replayComplete` event). So the `question` block records `live = !state.replaying` at the moment its event is processed. That single flag drives both the pinning and the inline read-only rendering.

## Components

1. **`web/src/session/reducer.ts`** — the `question` block gains `live: boolean`, set to `!state.replaying` in the `question` case. (No other reducer change; `_resolveQuestion` removal is unchanged.)

2. **`web/src/components/session/SessionView.tsx`** — compute the **first open question block with `live === true`** and render it in a new pinned panel **between `<SessionBody>` and `<Composer>`**, reusing `<QuestionCard>` (interactive), wired to `rt.onAnswer` / `rt.onChat` / `armedForChat`. "One at a time" needs no extra state: answering removes the block (`_resolveQuestion`), so the next open live question becomes first and pins.

3. **`web/src/components/session/TranscriptView.tsx`** — for `question` blocks: **skip** rendering a `live` block inline (it's pinned below — no duplicate); render a `!live` (replayed) block inline via `<QuestionCard readOnly>`.

4. **`web/src/components/session/QuestionCard.tsx`** — add a `readOnly?: boolean` prop. When set, render the header + question + options **non-interactively** (no submit/click handlers, visibly inert). Interactive mode unchanged.

5. **`web/src/components/session/SessionBody.tsx`** — the "Action needed — scroll up to respond" banner now fires **only for an open permission**, not a question (questions are pinned at the bottom, so there is nothing to scroll to). Permission handling is otherwise unchanged (out of scope to pin permissions).

## Answer → immediate send + affordance cleared (first-class requirement)

This must hold and is guarded by dedicated tests (the wiring exists today; the requirement is to not regress it):

- **Immediate send.** Answering — clicking an option, submitting freeform, or sending a chat-armed composer message — synchronously calls `rt.onAnswer(...)` → `answerQuestion(...)` → `socket._send({type:'answerQuestion',…})`. The WS send is un-batched and happens on the interaction; the bridge routes it to `orchestrator.answer_question` → the driver resolves the pending future → the turn unblocks and the answer reaches the model.
- **Affordance cleared.** `onAnswer` also dispatches `_resolveQuestion`, removing the block. The pinned panel re-derives from `rt.state.turns`, so the answered question disappears from the panel immediately; if no live-open question remains, the panel is gone. `hasOpenAction` recomputes, so any banner clears.
- **Multi-question.** Answering the first pinned question sends *its* answer immediately and advances the panel to the next open live question. The turn stays paused until all are answered (the driver `gather`s all futures) — correct behavior; the panel reflects it by showing the next question rather than disappearing.

## Known limitation (pre-existing, documented not fixed)

Reloading the page **while a question is pending** replays it as an inline read-only block (synthetic id) and it is **not re-answerable** — the live `questionId` the driver is blocking on isn't re-established on reattach. This change does not worsen that; re-establishing a live question on reattach is a separate, larger fix.

## Testing

- **reducer** — a `question` event during replay (`replaying=true`) → `block.live === false`; after `replayComplete` → `block.live === true`.
- **QuestionCard** — `readOnly` renders options with no interactive submit / no `onAnswer` wiring; interactive mode still answers.
- **TranscriptView** — a `live` question block is **not** rendered inline; a `!live` block renders read-only inline.
- **SessionView (pinned panel)** — the first live-open question renders in the pinned panel above the composer; a detached/replayed-only session shows **no** pinned panel.
- **Answer flow (the new requirement)** —
  - clicking an option in the pinned panel calls `onAnswer`/`answerQuestion` **synchronously** with the correct `(questionId, selected, mode)`;
  - after answering, the block is removed and the pinned panel + banner clear (no open action → gone);
  - with two open live questions, answering the first advances the panel to the second (panel not empty, turn not unblocked early);
  - the chat-armed composer path still answers (mode `chat`) and clears.

## Out of scope (YAGNI)

- Pinning open **permissions** (keep today's inline + banner).
- Re-answering a question after a mid-question page reload (the known limitation above).
- Any change to the freeform/chat escape-hatch behavior itself (reused as-is).
