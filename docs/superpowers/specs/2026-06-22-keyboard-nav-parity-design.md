# Keyboard Navigation Parity — Design

**Date:** 2026-06-22
**Status:** Approved (brainstorming complete)
**Feature area:** `web/` frontend keyboard navigation

## Goal

Move the ccswitch web UI toward keyboard-navigation parity with Claude Code. Two
concrete gaps were reported, plus a request to systematically find the rest:

1. **Sessions list ↑/↓** — arrow keys don't reliably move the highlight up/down the
   sessions list.
2. **In-session command history** — while in a session, ↑/↓ can't recall
   previously-sent prompts (bash / Claude-Code style history).

User decisions during brainstorming:
- **Scope:** fix these two now, then deliver a full keyboard-parity *audit* as a
  follow-up report (implementations of further bindings deferred for prioritization).
- **History persistence:** backend-persisted (survives reload, shared across devices).
- **Arrow conflict (issue 1):** reproduce and recommend — recommendation is the
  boundary model in Part A.

## Key findings (against `origin/master`, the auto-deployed code)

- The local worktree HEAD was **399 commits behind** `origin/master`. All work
  branches from `origin/master`; the design reflects deployed code, not the stale HEAD.
- **Issue 1 logic already largely exists** on `origin/master`:
  - `web/src/components/DispatchBar.tsx` handles ↑/↓ in the new-session textarea,
    navigating the list when the prompt is empty or the caret is at a line boundary
    (via `web/src/lib/caretLineEdges.ts`).
  - `web/src/components/Dashboard.tsx` (~lines 1218–1238) adds a global `document`
    keydown listener so ↑/↓ also move the highlight when focus is *outside* the
    prompt — bailing out under any `INPUT/TEXTAREA/SELECT` or open `role="dialog"`,
    and only while `activeTab === 'sessions'`.
  - Commit `6ecfb36` ("self-hosted auto-deploy from master") landed only recently, so
    the most probable cause of the reported failure is **deploy lag**: the running app
    predated this logic. The fix is verify-and-harden + regression tests, not a rewrite.
- **Issue 2 is genuinely net-new**, but cheaper than expected:
  - `web/src/session/reducer.ts` keeps `turns: Turn[]`; `role === 'user'` turns hold
    the exact submitted prompt text (`EvUser.text` → user block).
  - The backend **replays the full transcript on attach**, so user prompts are already
    persisted server-side and restored on reload/other devices. "Command history" is
    therefore satisfied by *deriving from existing transcript turns* — **no new DB
    table or endpoint required**, which fulfills the "backend-persisted" decision.

## Part A — Sessions list ↑/↓ (verify & harden)

### Target behavior (the recommended boundary model)
- **Empty or single-line prompt:** ↑/↓ move the list highlight (a single line has no
  caret destination, so there is no conflict).
- **Multi-line draft:** ↑/↓ move the caret between lines; only at the first/last line
  boundary do they cross into list navigation (existing `caretLineEdges` behavior).
- **Focus outside the prompt** (tab button, a session card, etc.): the global Dashboard
  listener moves the highlight, while bailing out under editable/form controls or an
  open dialog, and only on the Sessions tab.

### Work
1. Reproduce against current `origin/master` to determine whether a real bug remains
   (hypothesis: deploy lag) — follow `systematic-debugging` if a genuine defect appears.
2. Add **regression tests** (vitest) encoding each rule above:
   - empty prompt: ↑ and ↓ move the highlight;
   - single-line draft: ↑ and ↓ move the highlight;
   - multi-line draft, caret mid-text: ↑/↓ move the caret (highlight unchanged);
   - multi-line draft, caret at boundary: ↑/↓ move the highlight;
   - global listener: ↑/↓ move the highlight when focus is on a non-input element;
   - global listener bails when a `role="dialog"` is open and when focus is in an input.
3. Apply a minimal fix only if reproduction surfaces a genuine defect.

## Part B — In-session command history (net-new)

### Source of truth
`SessionView` derives `history: string[]` (oldest → newest) from reducer `turns`,
filtered to `role === 'user'`, extracting the user text block and skipping the
optimistic-seed turn (`OPTIMISTIC_USER_ID`) so a just-sent prompt isn't double-counted.
Persistence is inherited from transcript replay — no new storage.

### Composer recall behavior
Reuse `caretLineEdges` (shared with DispatchBar) for consistent boundary semantics:
- **↑** at the first line (or empty input) → recall the previous history entry.
- **↓** at the last line → recall the next entry, walking back toward the live draft.
- State: `historyIndex` where `-1` = live draft, plus a stashed `draft` string. Arrowing
  up from a draft stashes it; arrowing back down past the newest entry restores it.
- **Detach on edit:** any `onChange` (typing) resets `historyIndex` to `-1` (live), so a
  new edit is never silently treated as history navigation.
- **Reset on submit:** sending a prompt resets `historyIndex` to `-1`.
- **Yields to the slash-command palette:** when `pickingCommand` is active, the palette
  already owns ↑/↓ via `paletteRef.handleKeyDown` (the first check in `handleKeyDown`);
  history recall only engages when not picking a command.

### Edge cases
- Empty history: ↑ does nothing (no entry to recall).
- Recall then submit: submitting the recalled text appends it as a new user turn, which
  becomes the newest history entry (natural, matches Claude Code).
- Multi-line recalled entry: ↑/↓ first move the caret within it, crossing to the
  adjacent history entry only at the boundary line — consistent with the draft rules.

### Tests (vitest)
- ↑ on empty input recalls the most recent prompt; repeated ↑ walks older.
- ↓ walks back toward the draft and restores the stashed draft past the newest entry.
- Typing after a recall detaches (next ↑ starts from newest again).
- ↑/↓ do not recall history while the slash-command palette is open.
- Boundary behavior inside a multi-line recalled entry.

## Part C — Full keyboard-parity audit (follow-up report)

After A and B merge, produce `docs/superpowers/specs/2026-06-22-keyboard-parity-audit.md`:
a systematic comparison of Claude Code's keybindings against the ccswitch web UI. Each
binding is marked **present / partial / absent**, mapped to what is meaningful in a
browser chat UI (pure terminal-control bindings excluded), with a per-gap recommendation.
Delivered as a **prioritized list** for the user to choose from — **not implemented in
this PR**.

Candidate areas to assess (non-exhaustive): Escape behaviors (back / clear / cancel),
interrupt, permission-mode cycling (Shift+Tab, already present), transcript scrolling
(Page Up/Down, Home/End), queued-prompt navigation, slash-command completion (palette,
present), `@` file mentions, history search, and focus-trap correctness in modals.

## Sequencing & constraints

- **PR 1:** Part A + Part B, each developed test-first, lint/types/tests green.
- **Part C:** separate follow-up report.
- Branch from `origin/master` (done: `feat/keyboard-nav-parity`); follow the project
  completion workflow (commit → PR → merge). Governance: constitution v1.1.0 (TDD +
  quality gates, single source of truth / no dead code).
- Verification commands: `web/` → `vitest run`, `tsc --noEmit`, `eslint`.

## Out of scope

- New backend tables/endpoints for history (existing transcript suffices).
- Mobile/touch gestures (request is specifically about arrow keys).
- Rebinding/customization UI for shortcuts.
- Implementing additional parity bindings discovered in Part C (deferred to prioritization).
