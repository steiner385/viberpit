# Question Escape Hatches — Design

**Date:** 2026-06-21
**Feature area:** 001-ccswitch-web · in-session interactive prompts
**Status:** Approved (brainstorm), pending implementation plan

## Problem

When Claude calls `AskUserQuestion` inside a ccswitch session, the backend
(`driver.py:_handle_ask_user_question`) intercepts it and the frontend renders a
`QuestionCard` — single-select auto-submits, multi-select has a Submit button. The card
is a **closed choice**: the user can only click one of Claude's predefined options.

There must *always* be an escape hatch. When the user is presented with a question, they
must be able to **write a free-form response** or **chat about this** instead of being
forced to pick a predefined option.

### Key constraint

While a question is pending, the SDK's `can_use_tool` callback is **blocked awaiting an
`asyncio.Future`** (one per question, via `QuestionRegistry`). The SDK has no channel to
return an answer through `PermissionResultAllow`; the answer is delivered by resolving the
future, after which the driver returns `PermissionResultDeny(message=…)` whose `message`
the model receives as the tool-result text.

Therefore **any escape hatch must resolve that future**, or the turn hangs forever. A
plain Composer send (`send_prompt`) does not resolve it — it would queue a new prompt
while the turn stays blocked.

## Goals

- Every rendered `AskUserQuestion` card always offers two escape hatches in addition to
  Claude's options:
  1. **Write a response** — answer *this* question in the user's own words.
  2. **Chat about this** — set the rigid question aside and continue in normal conversation.
- Both paths correctly resolve the pending question future so the turn unblocks.
- The model receives intent-appropriate framing so it continues correctly (treats a
  free-form answer as an answer; treats a chat redirect conversationally).
- Existing fast tap-to-pick UX, accessibility (WCAG 2.1 AA), and mobile targets (44px) are
  preserved.

## Non-goals (v1)

- Permission prompts (`PermissionCard`) and plan review (`ExitPlanMode`) are **out of
  scope**. They already have natural non-binary paths ("keep planning", deny). They may be
  revisited later.
- No new persistence: question responses remain in-flight / UI-managed, as today.

## Chosen approach: Hybrid (B)

Two genuinely distinct paths:

- **Write a response** → handled **inline in the card**: a disclosure that expands a
  `<textarea>` + Send. Reuses the existing answer channel with a `freeform` mode tag.
- **Chat about this** → **routes to the main Composer**: collapses the card's options,
  returns focus to the Composer, and **arms a one-shot** so the user's next Composer
  message is delivered through `answerQuestion` (resolving the future) with a `chat` mode
  tag, instead of starting a new prompt.

### Approaches considered

- **A — Inline-only.** Both escape hatches expand a textarea in the card; only the framing
  tag differs. Simplest, fully contained. Rejected: the two options become nearly
  identical UX and "chat" doesn't feel like real conversation.
- **B — Hybrid (chosen).** Free-form inline, chat routes to Composer. Two distinct,
  intuitive paths; reuses existing plumbing; "chat" feels like real chat. Cost: one small
  coordination-state pointer in `SessionView`.
- **C — Composer-only.** Always type in the Composer, route to the pending question. Throws
  away the value of Claude's quick-pick options and the tap-to-answer UX. Rejected.

## Design

### Frontend — `web/src/components/session/QuestionCard.tsx`

Below Claude's options (for **both** single- and multi-select), always render:

- **"✍️ Write a response"** — a disclosure that expands an inline `<textarea>` + Send.
  On send → `onAnswer(questionId, [text], 'freeform')`. Empty text is ignored.
- **"💬 Chat about this"** — calls a new `onChat(questionId)` prop. The card collapses its
  options into a quiet "Replying in chat…" state with a way to cancel (re-show options),
  and focus moves to the Composer.

Preserve existing conventions: 44px min touch targets, `role`/`aria-labelledby`,
`focus-visible` rings, dark-mode classes.

### Frontend — `web/src/components/session/SessionView.tsx` + `Composer.tsx`

- `SessionView` holds `armedQuestionId: string | null`.
- `QuestionCard.onChat(qid)` sets `armedQuestionId = qid` and collapses the card.
- `Composer.onSend(text)`:
  - if `armedQuestionId` is set → `answerQuestion(sessionId, armedQuestionId, [text], 'chat')`
    and clear the arm (one-shot);
  - else → normal `send_prompt`.
- "Armed for chat" is cancelable (re-shows options) and **must clear on interrupt/Stop**
  and when the session detaches.

### Transport — `web/src/ws/client.ts` + `server/bridge/ws.py`

- Extend the `answerQuestion` WS message with optional
  `mode: 'option' | 'freeform' | 'chat'` (default `'option'`).
- Existing callers omit `mode` → behavior unchanged.

### Backend — `server/sessions/questions.py` + `driver.py`

- Thread `mode` through `QuestionRegistry.resolve` into the future's resolved value
  (e.g. resolve with `{selected, mode}` or a small tuple; keep `None` = dismissed).
- `driver.answer_question(question_id, selected, identity, mode='option')` and the
  orchestrator passthrough gain the `mode` parameter.
- In `_handle_ask_user_question`, choose the deny-message framing by mode:
  - `option` → `[User answered via the question UI] {Q} → {answer}` (unchanged)
  - `freeform` → `[User answered in their own words] {Q} → {text}`
  - `chat` → `[User wants to discuss rather than pick an option] {text}`
    (no rigid "Q → answer" shape, so the model replies conversationally)
  - dismissed (`None`) → unchanged.
- Multi-question (`questions` array) framing keeps its current per-line structure; each
  line uses the mode-appropriate phrasing.

### Data flow — chat path

1. User clicks **Chat about this** → card collapses, `armedQuestionId` set.
2. User types in the Composer and sends.
3. `answerQuestion(…, mode:'chat')` → WS → `orchestrator.answer_question` →
   `QuestionRegistry.resolve` fulfils the future.
4. The blocked `can_use_tool` callback returns `PermissionResultDeny(message=<chat framing>)`.
5. Turn unblocks; the model receives the user's message as conversational redirect and
   continues.

### Data flow — free-form path

1. User clicks **Write a response** → inline textarea expands.
2. User types and sends → `onAnswer(questionId, [text], 'freeform')` → WS →
   `QuestionRegistry.resolve` fulfils the future with `freeform` mode.
3. Turn unblocks; the model receives the typed text framed as a free-form answer.

## Testing (TDD)

- **Backend (`server/tests/`):** the three framing modes (`option`/`freeform`/`chat`)
  produce the expected deny-message text; single- and multi-question variants; dismissal
  still works; `mode` defaults to `option` when omitted.
- **Frontend reducer:** an armed question routes the next Composer send to `answerQuestion`
  (one-shot), and disarms on interrupt/detach.
- **`QuestionCard` component:** inline textarea submit calls `onAnswer(…, 'freeform')` and
  ignores empty input; "Chat about this" calls `onChat` and collapses options; escape
  hatches render for both single- and multi-select; a11y roles/labels present.

## Assumptions (confirmed during brainstorm)

1. v1 scope = `AskUserQuestion` cards only (not permission/plan cards).
2. Free-form answers inline; "chat" routes to the Composer (Approach B).
3. Both escape hatches appear for multi-select questions too.

## Backward compatibility

- `mode` is optional everywhere and defaults to `option`; the existing
  click-an-option and dismissal flows are unchanged.
- No DB/schema changes.
