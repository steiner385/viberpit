# Desktop-parity D-5: line comments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user comment on specific lines in the Changes pane and send all comments to Claude as one prompt.
The same line-comment component gives the plan-approval card per-step comments.

**Architecture:**
- A pure model in `web/src/features/sessions/review/reviewComments.ts` holds the comment types and the two prompt
  composers (review prompt, plan feedback).
- A per-session draft store keeps unsent comments across tab switches and reloads, persisted in `sessionStorage` under
  the existing storage-key namespace.
- A `LineCommentGutter` and `CommentEditor` pair plugs into `DiffRenderer` and the plan card.
- Sending uses the session's existing prompt path, so a busy session queues it like any typed prompt.
- **There is no new server endpoint.** Spec §10 lists `POST …/review-comments`, but the client-composed prompt is the
  same message through the one prompt path that already exists (ledger ruling).

**Tech Stack:** React + TS, vitest, Playwright. Web only.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2/§11 (D-5), §6 (plan comments). Decisions:
`docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D4, D17. Builds on D-4's `DiffRenderer`/`ChangesTab` and
D-2's plan-comment field.

## Global Constraints

- **Comment model:**

  ```ts
  type CommentAnchor = { path: string | null; side: 'new' | 'old'; start: number; end: number }  // inclusive line range on one side
  type LineComment = { id: string; anchor: CommentAnchor; quote: string[]; body: string }
  ```

  - `side` is `'new'` for add and context lines (numbered by `newNo`) and `'old'` for del lines (numbered by `oldNo`).
  - `quote` holds the anchored lines' text, captured when the comment is created, so a later diff refresh never loses
    what was commented on.
  - `path` is `null` for plan comments.
- **`composeReviewPrompt(comments)`** returns exactly this text:

  ```
  Review comments on the current changes (<N>):

  1. `<path>` line <start>[–<end>]<" (removed)" when side is old>:
     > <quote line 1>
     > <quote line 2>
     <body>

  2. …

  Address each comment, then summarize what you changed.
  ```

  - Comments are ordered by path, then side `new` before `old`, then start line.
  - The body is trimmed. Each quote line is prefixed with `> `, and quotes longer than 5 lines are cut to 5, followed by
    `> …`.
- **`composePlanFeedback(overall, stepComments)`** returns:
  - the trimmed overall text alone when there are no step comments;
  - otherwise the overall text (if any), a blank line, then `On the plan:` followed by the step comments, each as
    `- line <n>: "<quote>" — <body>`.

  The result must be at most 4000 characters, the server's deny-message cap. When it is longer, Send is disabled and
  the card shows `Too long — <len>/4000`.
- **Interaction:**
  - Each numbered diff line has a gutter button (`aria-label` `Comment on line <n>`). It is revealed on hover or focus,
    and always visible on coarse pointers.
  - Shift-clicking a second line on the same side within the same hunk extends the range. A shift-click anywhere else
    starts a new range.
  - The editor opens under the range end, with Save / Cancel. Ctrl/⌘+Enter saves and Esc cancels; an empty body
    can't be saved.
  - Saved comments render inline under their range with Edit and Delete.
- **Review bar:** a sticky bar in the Changes tab, shown when the draft has at least one comment: `<N> comment(s)`,
  `Discard all`, and `Send to Claude`.
  - Sending calls the same function the composer's Send/Queue uses, so it queues when busy. After a successful send
    the draft clears and a toast shows `Sent <N> comment(s)`.
  - `Discard all` asks for confirmation.
- **Draft persistence:** per session, in `sessionStorage` key `<namespace>reviewDraft:<sessionId>`. Use the existing
  storage-key helper (grep `storageKey`).
- **Bundle:** the gutter and editor are lazy inside `ChangesTab`'s chunk. The plan card's per-step mode loads them
  lazily as well, since the plan card is first-paint. Run the budget gate.
- **Gates:** typecheck, lint `--max-warnings 0`, full vitest, budget, plus the e2e spec.

## Review Focus

1. **A comment made, then the diff refetched** (the turn ended and lines shifted): the comment still renders with its
   quote, as a stale card above the file if its line no longer matches, and still sends. Pinned in Task 2.
2. **Sending while the session is busy:** the message goes through the queue path, not interrupt-and-send. Pinned in
   Task 2.
3. **Per-step plan comments plus overall text over 4000 characters:** Send is disabled with the length shown, and
   nothing is sent. Pinned in Task 3.
4. **Shift-click across a hunk boundary or across sides:** starts a new single-line range and never builds a range
   that mixes sides. Pinned in Task 2.
5. **Switching sessions:** drafts are per session and never leak between sessions. Pinned in Task 1.

---

### Task 1: Comment model, prompt composers, draft store

**Files:**
- Create: `web/src/features/sessions/review/reviewComments.ts`, holding the types, `composeReviewPrompt`,
  `composePlanFeedback`, `sortComments` and `newCommentId()`.
- Create: `web/src/features/sessions/review/useReviewDraft.ts`, returning
  `{ comments, add(c), update(id, body), remove(id), clear() }`, persisted per session.
- Test: `reviewComments.test.ts`, `useReviewDraft.test.ts`.

- [ ] **Step 1: Write failing tests**
  - **Exact prompt text:**
    - two files, a removed line, a range `12–14`, and a 7-line quote truncated to 5 plus `> …`;
    - ordering by path, then side, then start.
  - **`composePlanFeedback`:**
    - overall only → the trimmed overall text;
    - steps only → no leading blank line;
    - both → the exact text format.
  - **Draft store:**
    - add / update / remove / clear;
    - persists across a remount with the same `sessionId`;
    - a different `sessionId` sees an empty draft;
    - corrupt `sessionStorage` JSON → an empty draft, no throw.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the focused tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): review comment model, prompt composer and per-session draft (D-5)`

### Task 2: Line comments in the Changes pane

**Files:**
- Create: `web/src/features/sessions/review/LineCommentGutter.tsx` and `CommentEditor.tsx`. Both are presentational
  and receive callbacks.
- Modify: `DiffRenderer.tsx`. Add optional props `comments?: LineComment[]`, `onAddComment?(anchor, quote)`,
  `onEditComment?`, `onDeleteComment?`. When the props are absent (inline transcript diffs), it renders exactly as
  today. Saved comments render under their range. A comment whose anchor no longer matches a line renders as a stale
  card at the top of its file.
- Modify: `ChangesTab.tsx`. Wire `useReviewDraft(sessionId)`. Add the review bar, whose Send calls an `onSendReview`
  prop. `SessionBody` passes `onSendReview`, which uses the same runtime send/queue function the composer uses (find
  it in `SessionComposer`/`useSessionRuntime`).
- Test: `DiffRenderer.comments.test.tsx` and `ChangesTab.review.test.tsx`; extend `e2e/changes-tab.spec.ts` with
  "comment a line, send, assert the prompt request body".

- [ ] **Step 1: Write failing tests**
  - **Gutter:** clicking it opens the editor; Ctrl+Enter saves; the comment renders inline; Edit and Delete work.
  - **Ranges:** shift-click on the same side and hunk gives a range; shift-click across hunks or sides starts a new
    range.
  - **Removed lines:** a comment on a del line anchors `side: 'old'`.
  - **Review bar:** shows the count; Send calls `onSendReview` with the exact composed prompt and then the draft is
    empty; Discard asks first.
  - **Busy:** sending while busy uses the queue path (assert through the runtime mock).
  - **Stale anchor:** a refetched diff where the line moved renders the comment as stale and it is still sent.
  - **Inline diffs:** `DiffRenderer` without the comment props renders no gutter.
  - **E2E:** the prompt POST/WS frame carries the composed text.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, the budget and the e2e spec. Expected: PASS. Report the budget numbers; don't
  raise the ceilings.
- [ ] **Step 5: Commit** `feat(web): line comments in the Changes pane, sent as one prompt (D-5)`

### Task 3: Per-step plan comments

**Files:**
- Modify: `web/src/features/sessions/panels/PermissionCard.tsx`, the plan branch. In the "What should change?" mode,
  render the plan text (`block.input.plan`, falling back to the existing detail text) as numbered lines with the
  `LineCommentGutter`/`CommentEditor`, lazily loaded. Keep the overall textarea. Send & keep planning sends
  `deny` + `composePlanFeedback(overall, steps)`. Over 4000 characters, Send is disabled and `Too long — n/4000`
  shows.
- Test: `PermissionCard.planSteps.test.tsx`.

- [ ] **Step 1: Write failing tests**
  - **Step comment:** a comment on step line 3 plus overall text sends the exact composed message.
  - **Steps only:** no overall text sends the steps only.
  - **Over the limit:** more than 4000 characters disables Send and shows the length.
  - **Cancel:** sends nothing.
  - **Lazy load:** the plan card without comment mode does not import the editor (assert via a lazy-loading mock or
    the chunk boundary).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and the budget. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): per-step plan comments (D-5)`

### Task 4: Benchmark and docs

- In `scripts/desktop_parity_benchmark.py`, set `Changes pane: aggregate diff, unified/split, line comments` → `Y`,
  and update the `Plan approval with comments` note to `overall + per-step`.
- Regenerate audit §3.
- Document line comments and plan step comments in `docs/product/features/web-dashboard-mobile.md` (Changes
  paragraph) and `session-permissions.md` (plan approval).
- In spec §10, mark the `review-comments` endpoint row as "not needed — client-composed prompt (D-5)".
- If the controller has ruled to raise them, raise the bundle ceilings.
- [ ] Run the script. Expected: Target completion ≥ 74%.
- [ ] Commit `docs: D-5 benchmark rerun and line-comment docs`.
