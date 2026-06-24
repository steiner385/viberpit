# Grouped, batched multi-question panel — design

Date: 2026-06-23

## Problem

When the agent asks the user a multiple-choice question (Claude's `AskUserQuestion`
tool), the ccswitch web UI has three gaps:

1. **Option descriptions are never shown.** The tool's options carry a `description`
   per option, but the UI renders only the `label`.
2. **No way to revisit answers.** When one tool call asks several questions, the UI
   pins them one at a time and *sends each answer the instant it is picked*. There is
   no batch to review, so the user cannot go back and change an earlier answer before
   committing.
3. **Custom-reply text carries over.** After writing a freeform reply to one question,
   the typed text leaks into the next question.

## Root causes (verified in code)

- **Descriptions:** the backend `_normalize_option` (`server/sessions/driver.py:137`)
  passes each option dict through unchanged, so `description` already arrives on the
  wire as part of `options: Array<Record<string, unknown>>`. The frontend simply drops
  it — `optionLabel` (`web/src/components/session/QuestionCard.tsx:18`) reads only
  `opt.label`. **No backend/protocol change is needed.**
- **No batch:** each `AskUserQuestion` question is emitted as a separate `ev.Question`
  sharing a `turnId` (`driver.py:177-196`); the driver then `asyncio.gather`s all the
  answer futures (`driver.py:201-204`) and tolerates answers arriving in any order. But
  the frontend pins only the *first* live question (`PendingQuestionPanel`
  `findFirstLiveQuestion`) and `deliverAnswer` (`useSessionRuntime.ts`) sends over the
  WebSocket and resolves it immediately on selection. So there is no client-side batch.
- **Carryover:** `PendingQuestionPanel` renders `<QuestionCard block={q} … />` at a
  fixed tree position with **no `key`**. When `q` changes, React reuses the same
  component instance, so the `draft`/`writing` `useState` in `QuestionCard:129-130`
  persists across questions.

## Scope

All changes live in the React layer under `web/src/components/session/`. There are **no
backend, protocol, or wire-format changes** — the driver already forwards descriptions
and already awaits all answers together.

## Design

### 1. Render option descriptions

In `QuestionCard.tsx`, add an `optionDescription(opt)` helper alongside `optionLabel`.
Where each option renders its label (single-select buttons, multi-select checkboxes, and
the read-only/historical list), render a secondary muted line for the description when
present. Options without a description render exactly as today.

### 2. Grouped, batched submit (multi-question only)

- **New `QuestionGroup` component** between `PendingQuestionPanel` and `QuestionCard`.
  `PendingQuestionPanel` stops returning the first live question; instead it collects
  **all live questions sharing the latest pending `turnId`** and passes them to
  `QuestionGroup`. (Two `AskUserQuestion` calls within one turn would merge into one
  group — acceptable and rare.)
- `QuestionGroup` owns a local `answers` map: `questionId → { selected: string[],
  mode: 'option' | 'freeform' }`. Each child `QuestionCard` becomes **controlled** —
  selecting an option or typing a custom reply updates the group's local state and
  **highlights** the current choice instead of sending anything over the WebSocket.
- One **"Submit all answers"** button at the bottom of the group. On submit, the group
  fires the existing `onAnswer(questionId, selected, mode)` once per question; the
  driver's `asyncio.gather` collects them. **Submit stays disabled until every question
  in the group has an answer.**
- **Single-question case is unchanged and immediate.** When a tool call has exactly one
  question, picking an option sends right away, exactly as today. Batching applies only
  to 2+ questions, keeping the common path zero-friction.

### 3. Fix custom-reply carryover

- Add `key={block.questionId}` wherever a `QuestionCard` is rendered (the single-question
  pinned path and inside `QuestionGroup`), forcing a remount per question so
  `draft`/`writing` never leak.
- In the grouped flow, a successful submit resolves all questions and unmounts the group,
  so drafts are gone by construction. The `key` fix additionally protects the
  single-question sequential path.

### Escape hatches in grouped mode

- **"Write a response"** (freeform) stays **per question**: the typed text becomes that
  question's answer within the batch (mode `freeform`) and participates in the single
  submit.
- **"Chat about this"** (discuss in the composer) is inherently a whole-conversation
  bail-out. In grouped mode it collapses to one **group-level "Reply in chat instead"**
  that dismisses the group and arms the composer, rather than a per-question control
  (which would be ambiguous). Single-question mode keeps the existing per-question
  behavior.

## Components and responsibilities

- `QuestionCard` — renders one question. Gains: description rendering; a controlled mode
  (value + onChange supplied by a parent) used when inside a group, while retaining its
  current uncontrolled immediate-send behavior for the single-question path.
- `QuestionGroup` (new) — holds the local answers map for 2+ questions, renders one
  `QuestionCard` per question (each with a stable `key`), enforces "all answered before
  submit", and fires one `onAnswer` per question on submit.
- `PendingQuestionPanel` — selects the live questions for the latest pending `turnId`;
  renders a single `QuestionCard` (immediate-send) when there is one, or `QuestionGroup`
  when there are several.

## Error handling

- The existing disconnect buffering / auto-resend on reconnect in `useSessionRuntime`
  is per-`questionId` and unchanged; a batched submit simply enqueues several answers,
  each buffered and resent independently if the socket is down.
- Submit is inert (disabled) until all questions are answered, so partial batches cannot
  be sent.

## Testing

- `QuestionCard.test.tsx`: option descriptions render (and are absent when not provided);
  custom-reply `draft` does not survive a question change (key-driven remount).
- `PendingQuestionPanel.test.tsx` / `QuestionGroup` tests: single question sends
  immediately; a multi-question group sends nothing until Submit; Submit is disabled
  until all questions are answered; Submit fires exactly one `onAnswer` per question with
  the right selection/mode; changing an answer before submit updates what is sent.
- Gates: `vitest run`, `tsc -b --force`, `eslint` (the project's real type/lint gates).

## Out of scope

- Backend, protocol, or wire-format changes.
- A stepped/wizard presentation (the stacked layout was chosen).
- Changing single-question behavior.
