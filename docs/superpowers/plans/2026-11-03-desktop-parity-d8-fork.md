# Desktop-parity D-8: Fork from here — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork a Claude session from any message into a new linked child session that runs in its own worktree. Fork
from Claude's reply: the child keeps history up to and including that reply. Fork from your message: the child keeps
history up to the previous reply, and the composer is prefilled with that prompt. The `/fork` command forks at the
latest reply.

**Architecture:** Verified facts are in `docs/superpowers/specs/2026-11-02-d8-fork-verification.md` (CLI 2.1.316).
- **Fork invocation.** The child is spawned with
  `resume=<source sdk session id>, fork_session=True, extra_args={"resume-session-at": <assistant uuid>}`. The cut
  includes the anchor message. The flag is hidden, so it sits behind a kill switch.
- **Anchors.** The UI needs each turn's anchor uuid: the last top-level assistant transcript entry of that turn. The
  translator emits it live and replay emits it too.
- **Worktree.** The child gets a new worktree branched at the source's current HEAD, with the source's uncommitted
  tracked changes applied (`git stash create` + `stash apply`) and its untracked files copied.
- **Files are not rewound.** The child starts from the source's current files, and a callout says so. Restoring files
  is D-9 (rewind).

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §4 (message menu), §5 (Branching), §11 D-8.
Decision `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D3.

## Global Constraints

- **Engines.** Claude only. Set `AgentCapabilities.fork = True` for the Claude engine, and only when the kill switch
  `VIBERPIT_FORK_ENABLED` (default `true`) is on. Codex stays `fork=False`. The UI hides fork affordances when the
  capability is false.
- **Anchor uuid.**
  - The uuid of the **last top-level** (non-sub-agent) assistant transcript entry of a turn.
  - Emitted as a new normalized event `turnAnchor { turnId, uuid }` when a turn completes, live and on replay.
  - Stored on the web turn as `anchorUuid`.
  - A turn without an anchor (interrupted before any assistant entry, or an engine without anchors) shows no fork
    action.
- **Fork from a reply** (assistant turn T): `at = T.anchorUuid`.
- **Fork from your message** (user turn U, answered by assistant turn T):
  - `at` = the anchor of the assistant turn **before** U, and the child's composer is prefilled with U's text.
  - If U is the first message, there is no earlier anchor. The child is then a fresh session in the forked worktree
    with U's text prefilled, and no `--resume`.
- **API.**
  - Request: `POST /api/sessions/{id}/fork` with `{ "atTurnId": string, "mode": "reply" | "message" }`.
  - Response: `201 { "sessionId": newId, "prefill": string | null }`.
  - The server resolves the uuid from its own record of turn anchors. The client never sends a raw uuid.
  - Errors:
    - 404 for an unknown session.
    - 409 `FORK_UNAVAILABLE` when the kill switch is off or the engine can't fork.
    - 409 `TURN_NOT_FORKABLE` when the turn has no anchor.
    - 409 `daemon_outdated` from an old daemon.
- **Child session.**
  - `parentSessionId = source`.
  - Same account, engine, model, effort, and permission mode as the source.
  - Title: `Fork of <source title>`.
  - The new worktree branch is named `<prefix>fork-<short source id>-<n>`, using the existing branch-prefix rule (grep
    `branch_prefix` / `worktrees.py`).
  - The child lives on the same host as the source.
  - It shows up in the source's child-session rows (existing UI).
- **Worktree creation is read-only on the source.** `git stash create` writes objects only; never `stash push`, never
  touch the source index or working tree.
  - Tracked changes: `git stash apply <sha>` in the child.
  - Untracked files: copy files listed by `ls-files --others --exclude-standard` from the source, skipping anything
    that isn't a regular file.
  - Reuse the hardened git env from `worktree_diff`.
  - Any failure after the worktree exists deletes the new worktree. No half-made forks.
- **UI.**
  - **Fork from here** is added to the D-3 `MessageActions` menu: on assistant turns with an anchor, and on user turns
    whose fork target exists.
  - On success, navigate to the child session. With a prefill, set the child composer draft (the D-5/D-7 draft
    mechanism).
  - A toast reads `Forked — files start from the current worktree, not this message`.
  - `/fork` is an app command that forks at the latest reply.
- **Logging.** Expected refusals log at INFO. Errors carry `error_data` codes across RPC, as D-7 does.
- **Gates.** ruff, mypy, focused pytest; typecheck, lint, full vitest, budget, e2e.

## Review Focus

1. **The source must stay untouched.** The source worktree's index, working tree, and HEAD are byte-identical after a
   fork. Pinned in Task 2.
2. **A fork mid-turn.** Forking while the source is busy forks at the anchor of a completed turn only. The running turn
   has no anchor yet. Pinned in Task 1/3.
3. **A remote-host source.** The child is created on the same host, through the owner daemon. Pinned in Task 3.
4. **Kill switch off.** The capability is false, the UI hides fork, and the API returns 409 `FORK_UNAVAILABLE`. Pinned
   in Task 3/4.
5. **Replay after reload.** Anchors come back from the transcript so a reloaded session can fork from older turns.
   Pinned in Task 1.

---

### Task 1: Turn anchors (live + replay)

**Files:**
- Modify the translator (`server/sessions/translator.py`), `claude_driver.py`, replay (`server/sessions/replay.py`), and
  the event schema (`server/sessions/events*.py` and web `session/events.ts`).
- In the web reducer (`session/reducer.ts`), the turn gains `anchorUuid?: string`.
- The server's session record keeps `turn_anchors: dict[turnId, uuid]`, bounded to the last 500 turns. It is rebuilt
  on replay or resume. This is how the API resolves `atTurnId`.
- Tests: translator, replay, reducer, and record tests.

- [ ] **Step 1: Write failing tests**
  - A turn with thinking + text + a tool-use loop emits exactly one `turnAnchor`, carrying the uuid of the last
    top-level assistant entry.
  - Sub-agent entries are ignored.
  - An interrupted turn with no assistant entry emits no anchor.
  - Replay of a transcript JSONL yields the same anchors.
  - The reducer stores `anchorUuid` on the right turn.
  - The record keeps anchors bounded.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Take the SDK `AssistantMessage.uuid`, or `--replay-user-messages`, per the verification
  doc's "Implementation implications".
- [ ] **Step 4: Run** focused pytest + vitest, ruff, mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): turn anchors — the CLI message uuid each turn can fork from (D-8)`

### Task 2: Fork worktree (read-only on the source)

**Files:**
- Modify `src/viberpit/worktrees.py`: add
  `create_fork_worktree(source_cwd: str, *, branch: str, dest: str) -> str`, which returns the new worktree path. Reuse
  the existing worktree creation helpers and the hardened git env.
- Test: `tests/test_fork_worktree.py`, run with real temp repos and `PYTHONPATH=$PWD/src`.

- [ ] **Step 1: Write failing tests**
  - Committed, staged, unstaged, and untracked changes in the source all appear in the child worktree.
  - Ignored files are not copied.
  - The source's index, working tree, and HEAD are byte-identical before and after.
  - A symlink and a FIFO among the untracked files are skipped.
  - A failure partway through removes the half-made worktree.
  - The branch name follows the prefix rule.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and ruff. Expected: PASS.
- [ ] **Step 5: Commit** `feat(worktrees): fork worktree from a session's current state, read-only on the source (D-8)`

### Task 3: Fork orchestration + API

**Files:**
- Modify:
  - `server/sessions/engine.py`, Claude engine capability `fork`, gated by `VIBERPIT_FORK_ENABLED`. Follow the settings
    and env-var rules: new settings take the `VIBERPIT_` prefix.
  - Orchestrator: `fork_session(source_id, *, at_turn_id, mode) -> {sessionId, prefill}`. It resolves the anchor,
    creates the child worktree (Task 2), and creates the child record (parent, title, same account/model/mode/host).
    It spawns with `resume` + `fork_session=True` + `extra_args={"resume-session-at": uuid}`, or as a fresh session
    when there is no earlier anchor.
  - The SDK client factory accepts `fork_session` and `extra_args`.
  - RPC `_SIMPLE`, RemoteOrchestrator, fleet `_PER_SESSION`, and the facade.
  - `POST /{id}/fork` in `server/api/sessions.py`.
- Test: `server/tests/test_session_fork.py`, using a fake SDK factory to capture the spawn options and a real temp
  repo.

- [ ] **Step 1: Write failing tests**
  - Fork from a reply spawns the child with exactly `resume=<sdk id>`, `fork_session=True`, and
    `extra_args={'resume-session-at': uuid}`.
  - Fork from a message uses the previous anchor and returns that prompt as the prefill.
  - Fork from the first message: no resume, prefill set.
  - The child record has the right parent, title, account, model, mode, and host.
  - An unknown turn returns 409 `TURN_NOT_FORKABLE`.
  - Kill switch off returns 409 `FORK_UNAVAILABLE`, and the capability is false.
  - Codex returns 409 `FORK_UNAVAILABLE`.
  - Over a real UDS RPC round trip the child is created on the owner daemon, and no WARNING logs appear for expected
    refusals.
  - A failure after the worktree exists removes the worktree and the child record.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** focused pytest and the RPC parity tests, ruff, mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): fork a session from a turn into a linked child (D-8)`

### Task 4: UI — Fork from here + /fork

**Files:**
- Modify:
  - `web/src/features/sessions/transcript/MessageActions.tsx`: add the item, gated by the capability and an anchor.
  - `web/src/api/hooks.ts`: add a `useForkSession` mutation, and update the explicit mocks.
  - Navigate to the child and apply the prefill draft.
  - Show the toast.
  - `server/sessions/commands/catalog.py`: add the `/fork` app command (forks at the latest reply). Wire it through the
    runtime `onCommand`.
- Test: `MessageActions.test.tsx`, a hook test, and an e2e `web/e2e/fork.spec.ts` (route-mocked).

- [ ] **Step 1: Write failing tests**
  - An assistant turn with an anchor shows **Fork from here**. Clicking it POSTs `{atTurnId, mode:'reply'}`,
    navigates to the new id, and shows the toast.
  - The user-turn variant uses `mode:'message'` and sets the child draft to the prefill.
  - No anchor, or a session without the capability: no item.
  - A 409 shows an error toast.
  - `/fork` forks at the latest reply.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, budget, and e2e. Expected: PASS. Report the budget.
- [ ] **Step 5: Commit** `feat(web): Fork from here in the message menu, and /fork (D-8)`

### Task 5: Benchmark and docs

- In `scripts/desktop_parity_benchmark.py`, set `Fork session (from here)` → `Y`, with the note
  `Claude; files start from current worktree`.
- Regenerate audit §3.
- Document fork in the product docs.
- In spec §5, record the verified invocation and the from-your-message semantics.
- Raise the bundle ceilings only if ruled.
- [ ] Run the script. Expected: Target completion ≥ 80%.
- [ ] Commit `docs: D-8 benchmark rerun and fork docs`.
