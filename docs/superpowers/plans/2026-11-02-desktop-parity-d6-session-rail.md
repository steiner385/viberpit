# Desktop-parity D-6: session rail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dense session meta strip with a right-hand **session rail** of four cards (Changes, Context,
Environment, Tasks). When the session pane is narrow (phone, tablet, or desktop with a pushed drawer), the rail
becomes a **Details** tab. What remains of the strip becomes a slim toolbar.

**Architecture:**
- **Server:** the browser `Session` gains `branch` and `cwd`. Both already exist on the session record.
- **Cards:** four presentational card components reuse the existing pieces: `useSessionDiff`, `McpServerList`,
  `SessionRulesReadout`, `SubAgentList`, `BackgroundWorkList`, `PrList`, `CopyablePath`, and the context helpers.
- **Layout:** `SessionRail` places the cards. It switches on the session pane's own width using a container query,
  never `useViewport`, so a pushed assistant drawer collapses it the same way a phone does.
- **`SessionMeta` is deleted.** Its status / view-mode / export items move to `SessionToolbar`. Everything else moves
  into the rail cards. The Details fold goes away.

**Tech Stack:** FastAPI + pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 (rail) and §11 row D-6. Decisions:
`docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D12, D16, D17.

## Global Constraints

- **Card titles and order:** **Changes**, **Context**, **Environment**, **Tasks**. Each card is a `<section aria-labelledby>`
  with an `h3` title.
- **Changes card:**
  - Shows `+N −M · K files`, the first 5 files (path, status, +/−), and an **Open Changes** button that calls
    `setView('changes')`.
  - Reads from `useSessionDiff(id, { summary: true, busy })`, the same query key as the header chip.
  - With no files it shows `No changes yet`.
- **Context card:**
  - A meter bar labelled `Context N% used`, using the same numbers as the prompt-row ring (`rt.contextPct`).
  - At `contextSeverity === 'danger'`, a visible line `Auto-compacts soon` plus a **Compact now** button, which sends
    `/compact` through `rt.onPlainPrompt`. Show the button only when `rt.commands` contains `compact`.
  - The MCP server list is embedded inline (`McpServerList`, with no popover) together with a `Manage →` link.
  - The rules readout (`SessionRulesReadout`).
  - The quota windows (`rt.quota`).
- **Environment card:**
  - Engine (non-default only), the account select plus Unpin (same handlers as today), host, display mode
    (desktop/virtual + reason).
  - **Branch** (`session.branch`, or `—`) and **Worktree** (`session.cwd` via `CopyablePath`, or `—`).
  - PR pills (`PrList` over `session.prs`).
- **Tasks card:** sub-agents (`SubAgentList`, gated on `can(caps,'subagents')`) and background work
  (`BackgroundWorkList`), rendered inline. With neither, it shows `No background tasks`.
- **Layout:**
  - The rail is a right column inside the session page, below the header and beside the tabs/transcript. The
    composer dock still spans the full width below both.
  - It shows when the session pane's container width is **≥ 64rem**. Below that, the cards render in a **Details** tab
    (`?view=details`), last in the tab strip.
  - The tab strip is always shown below 64rem, because Details always exists there.
  - On ≥ 64rem the rail can be collapsed. A toggle in `SessionToolbar` (`aria-label` `Show details` / `Hide details`,
    `aria-expanded`) is persisted per browser under the storage-key namespace (`sessionRail.collapsed`).
  - Rail width is `20rem`. It scrolls independently, and the transcript keeps `min-w-0`.
- **`SessionToolbar`** (replaces `SessionMeta`) holds the StatusBar, ViewModeMenu, Export and the rail toggle. Nothing
  else. Delete `SessionMeta.tsx`, its fold and its dead `actionsSlot` prop in the same PR, and move or rewrite its
  tests.
- **URL state:** `details` is added to `sessionView.ts` as the single owner of `?view=`. On ≥ 64rem, `?view=details`
  falls back to `conversation`, the same way an unlisted tab does today.
- **No new server endpoints.**
- **Gates:** typecheck, lint, full vitest, budget, and e2e. Server: ruff, mypy, focused pytest.

## Review Focus

1. **Assistant drawer pushed at 1280px:** the rail collapses into the Details tab with no horizontal overflow. Pinned
   in Task 3's e2e.
2. **Account switch from the Environment card:** it behaves exactly as the old strip did (`rt.onCommand('account')`,
   Unpin mutation). Pinned in Task 2.
3. **`/compact` on an engine without the command:** the button is hidden. Pinned in Task 2.
4. **Ended or archived sessions** (diff 404): the Changes card shows `No changes yet` and never retries in a loop.
   Pinned in Task 2.
5. **Phone:** the Details tab holds all four cards; the tab strip doesn't overflow at 390px. Pinned in Task 3's e2e.

---

### Task 1: Server — branch and worktree path on the browser Session

**Files:**
- Modify: `server/api/sessions.py` `_record_to_dict`. Add `"branch": record.branch` and `"cwd": record.cwd`, as
  nullable strings. Follow the existing field style.
- Modify: `web/src/api/types.ts` `Session`. Add `branch?: string | null` and `cwd?: string | null`.
- Check: any snapshot or contract tests over the Session payload (`grep -rn "_record_to_dict\|displayFallbackReason" server/tests | head`).
- Test: `server/tests/test_session_payload_branch_cwd.py`.

- [ ] **Step 1: Write failing tests**
  - GET a session whose record has `branch='feat/x'` and `cwd='/w'` → the JSON has both.
  - A record with no branch → `"branch": null`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus the session API suites touched by the payload; ruff; mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): expose branch and worktree path on the session payload (D-6)`

### Task 2: The four rail cards

**Files:**
- Create in `web/src/features/sessions/rail/`: `ChangesCard.tsx`, `ContextCard.tsx`, `EnvironmentCard.tsx`,
  `TasksCard.tsx`, `RailCard.tsx` (the shared section and heading chrome), and `index.ts`.
- Each card takes explicit props: the pieces of `rt`/`session` it reads, plus callbacks. No card imports
  `useSessionRuntime` directly. `ChangesCard` may call `useSessionDiff` itself.
- Test: one test file per card, in `rail/*.test.tsx`.

- [ ] **Step 1: Write failing tests**
  - **ChangesCard:**
    - Shows totals and at most 5 files.
    - **Open Changes** calls `onOpenChanges`.
    - A 404 or empty diff shows `No changes yet`.
  - **ContextCard:**
    - The meter label matches the ring's numbers.
    - At danger: shows `Auto-compacts soon`, and **Compact now** calls `onPlainPrompt('/compact')`.
    - With no `compact` in commands, the button is hidden.
    - The MCP list and Manage link render inline.
    - The quota windows render.
  - **EnvironmentCard:**
    - The account select calls `onSelectAccount`, and Unpin appears when pinned.
    - Shows host, display mode and reason.
    - Branch and worktree show values, or `—`.
    - Shows PR pills.
  - **TasksCard:**
    - Sub-agents appear only with the capability.
    - Background rows render.
    - With neither, shows `No background tasks`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse the existing components and helpers; no copies.
- [ ] **Step 4: Run** the focused tests, typecheck, and lint. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): session rail cards — Changes, Context, Environment, Tasks (D-6)`

### Task 3: Rail layout, Details tab, toolbar; delete SessionMeta

**Files:**
- Create: `web/src/features/sessions/rail/SessionRail.tsx`, which renders the cards in order and is fed from
  `SessionBody`.
- Create: `web/src/features/sessions/SessionToolbar.tsx`, with the status, view mode, export, and rail toggle.
- Modify:
  - `SessionBody.tsx`: a row wrapper that puts the tabs and transcript beside the rail; the Details tab; the
    container query; `?view=details`.
  - `MainPaneSession.tsx` if it owns the wrapper.
  - `sessionView.ts`: add `details`.
- **Delete:** `SessionMeta.tsx`, `SessionMeta.test.tsx`, `SessionMeta.fold.test.tsx`, `SessionMeta.switchers.test.tsx`.
  Port the assertions that still apply to the card and toolbar tests. Update the tests that mock the meta slots
  (`SessionBody*.test.tsx`, `MainPaneSession.meta.test.tsx`, `sessionWorkspace.test.tsx`).
- Test: `SessionRail.test.tsx`, `SessionToolbar.test.tsx`, the `SessionBody` layout tests, and the e2e
  `web/e2e/session-rail.spec.ts` (mock the API with `page.route`, as the changes-tab spec does).

- [ ] **Step 1: Write failing tests**
  - **Wide pane (≥ 64rem, simulated in jsdom via the container-width hook or a class toggle):**
    - The rail renders all four cards, and there is no Details tab.
    - The toggle collapses the rail, and the choice persists.
  - **Narrow pane:**
    - No rail; a Details tab lists all four cards.
    - `?view=details` on a wide pane falls back to conversation.
  - **SessionToolbar:** holds only the status, view, export, and toggle.
  - **Cleanup:** no `SessionMeta` import remains (grep test).
  - **E2E** (all with no horizontal overflow, measuring the pane's `scrollWidth`):
    - 1440 → rail visible;
    - 1280 with `?drawer=assistant` → Details tab;
    - 390 → Details tab, with the strip fitting.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, the budget, and the e2e. Expected: PASS. Report the budget numbers; don't
  raise the ceilings.
- [ ] **Step 5: Commit** `feat(web): session rail replaces the meta strip; Details tab on narrow panes (D-6)`

### Task 4: Benchmark and docs

- In `scripts/desktop_parity_benchmark.py`:
  - `Session rail: Changes / Context / Environment / Tasks cards` → `Y`.
  - `Context meter + 'auto-compacts soon'` → `Y`.
  - `Worktree visible + controls (branch, enter, discard)` stays `P`, with the note
    `branch + path shown; discard/repair → D-14`.
- Regenerate audit §3 from the script output.
- Update the product docs for the session view: replace the meta strip description with the rail and the Details tab.
  Note in spec §2 that the rail switches on pane width (container query), not viewport width.
- Raise the bundle ceilings only if the controller rules so.
- [ ] Run the script. Expected: Target completion ≥ 75%.
- [ ] Commit `docs: D-6 benchmark rerun and session-rail docs`.
