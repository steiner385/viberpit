# Desktop-parity D-19: adopted leftovers — error-card recovery, worktree discard/repair, ⌘K start-on-branch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** finish the benchmark rows that the owner already adopted but that are still partial:
- **Error-card recovery (D17).** Add "Switch model" and "Lower effort" next to the existing "Rewind & retry".
- **Worktree controls (D16).** Add **Discard** and **Repair** to the Environment card.
- **⌘K branches (D5).** Add "New session on `<repo>`@`<branch>`".
- **Effort row.** The benchmark note for the effort picker is stale; #3260 shipped in #3269.

**Decisions:** D5, D16 and D17 in `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` (all "Adopt"). The spec is `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md`: lines ~80–81 cover Environment actions, ~302–303 cover error cards.

**Current-state map:** read the "Facts (2026-11-06)" section below. It has `file:line` for every surface.

**Controller rulings (2026-11-06):**
- **Discard** means "end this session and remove its worktree". It is allowed only when the session is idle. It requires a person, a confirm dialog and an audit row.
  - Before removal, always write recovery refs: `snapshot_recovery_ref` for a dirty tree, and `snapshot_branch_tip` for unpushed commits.
  - Add the reason `discard` to the allowed recovery reasons.
  - The confirm dialog shows dirty and unpushed counts and the restore command.
  - The session is archived as part of the discard.
  - Only a session branch matching `brand.is_session_branch` is deleted, which `remove_worktree` already enforces.
  - A non-managed cwd (not a managed worktree) can't be discarded.
- **Repair** runs the existing `_ensure_session_cwd` path on demand. It only recreates a missing or empty managed worktree and never touches a present tree. It requires a person and writes an audit row.
- **Error kinds.** The server adds `kind` to the `error` event, using one classifier that reuses `server/sessions/failure.py` `classify()` and its codes. The kinds are:
  - `rate_limit`
  - `overloaded`
  - `context_too_long` (Claude `api_400` whose message says the prompt is too long; Codex `contextWindowExceeded`)
  - `model_unavailable` (`api_404`, model-not-found text)
  - `other`

  The web keys card actions off `kind`. Map them as follows:
  - `rate_limit` and `overloaded`: Switch model, Lower effort, Rewind & retry.
  - `context_too_long`: Rewind & retry, plus a "Compact" hint if `/compact` exists. Grep before adding it.
  - `model_unavailable`: Switch model.
  - `other`: Rewind & retry.
- **Switch model** opens the existing model picker through `usePromptRowMenusHandle().openModel()`. It never auto-picks a model.
- **Lower effort** steps down one level in the session's `supportedEffortLevels`, or in the catalog efforts for Codex, through the existing `onSetModel(model, when, effort)` path. It is hidden at the lowest level or when the engine has no effort.
- **⌘K start-on-branch.**
  - Palette branch entries carry the `(repo, branch)` pair.
  - A new `startOnBranch` host method opens the dispatch bar with repo and branch prefilled, using the same mechanism as `presetRequest`.
  - DispatchBar gains a branch field in `onDispatch`, shown as a removable chip.
  - `PromptDock.handleDispatch` sends the chosen branch instead of always sending `defaultBranch`.
  - The server already accepts `branch` and returns 409 `branch_switch_failed`; surface that error.

## Facts (2026-11-06, origin/master `812dbf55`)

- **ErrorCard and reducer.**
  - `web/src/features/sessions/transcript/ErrorCard.tsx:24-62` has only "Rewind & retry", gated by `useRewindActions()` and `retryablePrompt`.
  - It is rendered at `Turn.tsx:180`.
  - The error block is built in `session/reducer.ts:1440-1460` with `code`, `message` and `resetsAt`.
- **Error codes.**
  - Claude: `server/sessions/translator.py:466-504` `_error_from_result` produces `api_<status>`, `max_turns`, `execution_error` and `agent_error`.
  - Classifier: `server/sessions/failure.py:166` `classify()`.
  - Codex: categories are in `server/codex/schema.py:3386`, and the driver emits them at `server/codex/driver.py:1997`.
  - `ErrorEvent` is at `server/sessions/events.py:400`.
- **Model and effort.**
  - `SessionRuntime.onSetModel(id, when, effort?)` is at `session/useSessionRuntime.ts:132` and `:615-634`.
  - The route is `POST /sessions/{id}/model` (`server/api/sessions.py:2610`). It is human-gated and returns `SETTINGS_INVALID` for a bad level.
  - The PromptRow effort picker is at `composer/PromptRow.tsx:60-68,112`.
  - `PromptRowMenus.openModel/openEffort` is at `PromptRow.tsx:77-82`. It is exposed via `usePromptRowMenusHandle()` (`shell/sessionGestures.tsx:195`) and used by `AppShell.tsx:375-376`.
- **Worktree.**
  - `EnvironmentCard.tsx:78-83` shows branch and path only. Props are at `:20-40`, fed from `SessionBody.tsx:169-180`.
  - `src/viberpit/worktrees.py`:
    - `remove_worktree` (`:1333`);
    - `snapshot_recovery_ref` (`:1184`, allowed reasons `drain`, `delete`, `forced`);
    - `snapshot_branch_tip` (`:1271`);
    - `recovery_refs` (`:1143`);
    - `restore_command` (`:1131`);
    - `recreate_worktree` (`:1293`).
  - `orchestrator.py`:
    - `_cleanup_session_paths` (`:6823`);
    - `close_session` (`:6865`);
    - `_ensure_session_cwd` (`:6746`, refusal reasons via `_note_worktree_missing` `:6732`).
  - `worktree_transfer.inspect` (`:90`) and `_count_unpushed` (`:114`) supply dirty and unpushed facts.
  - The `/reload` route pattern is at `server/api/sessions.py:~2763`.
- **Palette and dispatch.**
  - `usePaletteEntries.ts:161-176`: branches come from live sessions only, with no repo.
  - `paletteHost.ts:26-34`; `AppShell.tsx:462-463,569,692-697` (`presetRequest`).
  - `DispatchBar.tsx:55,137,333,431` has no branch field.
  - `PromptDock.tsx:224,247` always sends `branch: repoInfo?.defaultBranch`.
  - `CreateSessionRequest.branch` is at `server/api/sessions.py:109`; `branch_switch_failed` is at `:1113-1121`.
- **Benchmark rows** (`scripts/desktop_parity_benchmark.py`): line 17 (palette), 44 (effort), 80 (errors), 84 (worktree).

## Global Constraints

- **Server additions:**
  - `kind` on the `error` event, plus replay (check that persisted events carry it, or compute it on read).
  - `POST /api/sessions/{id}/worktree/discard {confirm}` and `POST /api/sessions/{id}/worktree/repair`.
  - `GET /api/sessions/{id}/worktree/preflight`, returning `{dirty, unpushed, managed, present, restoreHint}`.
  - All of these are per-session RPCs on the owning daemon (`_PER_SESSION`, `orchestrator_rpc._SIMPLE`, remote, facade, fleet).
  - The mutations need a person (`require_human`), audit rows `session.worktree_discard` and `session.worktree_repair`, and INFO logging for refusals.
  - Discard and repair are blocked while a move is in progress (`_MOVE_BLOCKED`).
- **Reuse, don't duplicate:**
  - `failure.classify`, `remove_worktree`, the snapshot helpers, `_ensure_session_cwd`, `worktree_transfer.inspect`, `onSetModel`, `usePromptRowMenusHandle` and the `presetRequest` mechanism.
- **Gates:**
  - Server: ruff, mypy, focused pytest plus parity, the full `server/tests -n 4` with `PYTHONPATH=$PWD/src` in a clean env, and core `tests/` (`worktrees.py` is in `src`).
  - Web: typecheck, lint, the full vitest, build, budget, e2e.
- **Budget:** ceilings are 111.0 / 308.2 / 15.6 and initialJs is at about 308.0, so headroom is thin. Keep new UI in lazy modules (the ErrorCard actions menu, the discard dialog). The palette and dispatch changes are small; measure them.

## Review Focus

1. Discard never loses work silently. Recovery refs exist for any dirty tree or unpushed commits, and only session-prefixed branches are deleted (Task 1).
2. Discard and repair are refused for a busy session, during a move, for a non-managed cwd, or from a token caller (Task 1).
3. Repair never modifies a present tree (Task 1).
4. Error kinds map correctly for Claude and Codex codes. "Lower effort" is hidden at the floor (Tasks 1 and 2).
5. Start-on-branch sends the chosen branch, and a `branch_switch_failed` is shown (Task 2).

---

### Task 1: Server — error kind, worktree preflight/discard/repair

**Files:**
- `failure.py` (or a thin `error_kind()` next to it)
- `translator.py`, the Codex translator, and `events.py` (`kind`)
- the orchestrator `worktree_preflight`, `discard_worktree` and `repair_worktree` methods
- `worktrees.py` (the `discard` reason)
- RPC, remote, facade and fleet
- `session_actions`
- `api/sessions.py` routes

**Tests:** `server/tests/test_worktree_controls.py`, `server/tests/test_error_kind.py`, and core `tests/test_worktrees*.py`.

- [ ] **Step 1:** write failing tests for every bullet, plus Review Focus 1–4 on the server side.
- [ ] **Step 2–4:** focused tests, parity, ruff, mypy, core.
- [ ] **Step 5:** commit `feat(sessions): error kinds, worktree discard/repair with recovery refs (D-19)`

### Task 2: Web — error-card actions, Environment controls, ⌘K start-on-branch

**Files:**
- `ErrorCard.tsx`: actions by `kind`, with a lazy actions module.
- `Turn.tsx` / `TranscriptView.tsx`, or a context to reach the runtime and menus.
- `reducer.ts`: keep `kind`.
- `EnvironmentCard.tsx`: Discard and Repair, with a lazy confirm dialog that runs the preflight.
- `usePaletteEntries.ts`, `paletteHost.ts`, `AppShell.tsx`, `PromptDock.tsx`, `DispatchBar.tsx`: branch support.
- Hooks and mocks.

**Tests:** unit tests, plus e2e `web/e2e/parity-leftovers.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1:** write failing tests for Review Focus 4–5 on the web side, the discard dialog's counts and restore hint, repair, and the ⌘K entry.
- [ ] **Step 2–4:** run the full web gates and e2e.
- [ ] **Step 5:** commit `feat(web): error-card recovery, worktree discard/repair, start a session on a branch from ⌘K (D-19)`

### Task 3: Benchmark and docs

- **Benchmark:**
  - Rows 80, 84 and 17 → Y if they're fully covered; otherwise give an exact note.
  - Row 44's note: per-model levels shipped in #3269. Set it to Y if nothing else is missing; otherwise state precisely what is.
  - Regenerate §3 and report the target (D-18 was 96%).
- **Spec:** record the rulings above in the relevant rows.
- **Product docs:** cover the session rail and the transcript.
- **Ceilings:** measured + 0.3, and only if they moved.
- [ ] Commit `docs: D-19 benchmark rerun and parity-leftovers docs`.
