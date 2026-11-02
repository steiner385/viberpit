# Desktop-parity D-3: message menu and transcript view modes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Desktop's transcript view modes (Normal / Thinking / Verbose, cycled with Ctrl/⌘+O) and a per-message
action menu whose first item is **Copy as Markdown**.

**Architecture:**
- View modes are a pure mapping onto the two existing render knobs (`toolDetailMode`, `thinkingCollapsed`). The
  Settings values define **Normal**, so no new persisted setting and no server change.
- The current mode is per-session client state on the runtime.
- The message menu is one `MessageActions` component on each turn, built on `ui/Menu`. Fork/Rewind/Edit (D-8, D-9)
  will add items to it later.

**Tech Stack:** React + TS, vitest. Web only.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §4 (view modes, message menu) and §11
row D-3. Decisions: `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D7, D13.

## Global Constraints

- Mode names and order are exact: **Normal**, **Thinking**, **Verbose**. Ctrl/⌘+O cycles
  Normal → Thinking → Verbose → Normal.
- Mapping, where `S` is the user's Settings (`defaultToolDetail`, `thinkingCollapsed`):

  | Mode | `toolDetailMode` | `thinkingCollapsed` |
  |---|---|---|
  | Normal | `S.defaultToolDetail` | `S.thinkingCollapsed` |
  | Thinking | `S.defaultToolDetail` | `false` |
  | Verbose | `'all-open'` | `false` |

- The mode is per session and resets to Normal when a different session opens. It is not persisted.
- Changing the mode must override a block the user toggled by hand only on an explicit mode change. Today
  `ThinkingBlock` keeps a manual toggle forever. The rule: a mode change re-applies the mode's defaults to every
  block, and later manual toggles stick until the next mode change.
- Message menu: one trigger per turn (aria-label `Message actions`). It is visible on hover or focus-within on
  pointer devices and always visible on coarse pointers. Its first item is **Copy as Markdown**.
- What Copy as Markdown copies:
  - an assistant turn: its `text` blocks joined with a blank line (tools, thinking and notices excluded);
  - a user turn: the prompt text;
  - an image-only user turn: nothing (the item is hidden).

  Success shows the toast `Copied`. A failed clipboard write shows `Couldn't copy`.
- Ctrl/⌘+O must be registered in `shell/shortcuts.ts` only. It fires while typing (a chord is not a character), only
  with a session open, interactive, and no overlay open. Use the existing `cmdOrCtrl` helper and wire it through the
  gesture facts, as D-1/D-2 did.
- Gates: typecheck, lint `--max-warnings 0`, full vitest, plus `(cd web && npm run build && npm run budget); rm -rf web/dist`.

## Review Focus

1. **Thinking or Verbose while a turn streams:** a new thinking block arriving mid-stream must render per the current
   mode. Pinned in Task 1.
2. **A block manually collapsed in Verbose, then the mode cycles back to Verbose:** it re-opens, because the mode
   change re-applies. Pinned in Task 1.
3. **Copy on an assistant turn with only tool calls (no text):** the item is hidden, never an empty clipboard. Pinned
   in Task 2.
4. **Ctrl+O in the assistant drawer's composer:** must not cycle the page session's mode. The guard requires the
   page session and no overlay; the drawer counts as an overlay or the row checks focus. Pinned in Task 1.
5. **Copy on a turn rendered by the archived read-only reader** (`ReviewPage`): it must still work, since it is
   read-only and copy is harmless. Pinned in Task 2 if `Turn` is shared, otherwise noted.

---

### Task 1: View modes (resolver, control, Ctrl/⌘+O)

**Files:**
- Create: `web/src/session/viewMode.ts`, holding `TRANSCRIPT_VIEW_MODES`, `type TranscriptViewMode`,
  `nextViewMode(m)` and `resolveViewMode(mode, settings)`.
- Modify: `web/src/session/useSessionRuntime.ts`:
  - `viewMode` state, reset when the session id changes, plus `setViewMode` and `cycleViewMode`;
  - `toolDetailMode` and `thinkingCollapsed` returned from `resolveViewMode`;
  - a `viewModeEpoch` number that increments on each mode change.
- Modify: `ThinkingBlock.tsx` and the tool-result toggles in `ToolGroup`/`ToolCallCard`, which take `resetKey`
  (the epoch). A changed `resetKey` clears the manual-toggle state.
- Create: `web/src/features/sessions/transcript/ViewModeMenu.tsx`. It sits in the session meta strip's right cluster,
  next to Export (`SessionMeta.tsx` `exportSlot` area). The trigger label is `View: <Mode>`.
- Modify: `web/src/shell/shortcuts.ts`:
  - add the row `insession.cycleView`, keys `Ctrl+O`, `keysMac` `⌘O`, matched with `cmdOrCtrl(e.key === 'o' || e.key === 'O')`;
  - `when: c.sessionOpen && c.interactive && !c.overlayOpen`;
  - wire `cycleViewMode` through `shell/sessionGestures.tsx` facts from `MainPaneSession`.
- Test: `viewMode.test.ts`, `ViewModeMenu.test.tsx`, `ThinkingBlock.test.tsx`, `useShortcuts.test.tsx`, plus a
  `Turn`/`TranscriptView` test for the streaming case.

**Interfaces:**
- Produces:
  - `type TranscriptViewMode = 'normal' | 'thinking' | 'verbose'`
  - `resolveViewMode(mode: TranscriptViewMode, s: { defaultToolDetail: ToolDetailMode; thinkingCollapsed: boolean }): { toolDetailMode: ToolDetailMode; thinkingCollapsed: boolean }`
  - On `SessionRuntime`: `viewMode`, `setViewMode(m)`, `cycleViewMode()`, `viewModeEpoch`.

- [ ] **Step 1: Write failing tests**
  - **Resolver:** `resolveViewMode` returns the mapping table exactly, including Normal following `S` when
    `S = {all-closed, false}`.
  - **Cycle:** `nextViewMode` goes normal → thinking → verbose → normal.
  - **Menu:** it lists the three modes, checks the current one, and calls `setViewMode` on select.
  - **ThinkingBlock reset:**
    - closed by default under Normal, open after the epoch bump to Thinking;
    - a manual collapse sticks until the next epoch.
  - **Ctrl+O:** cycles the open session's mode; while typing in the session composer it still cycles; with the
    assistant drawer open it does nothing.
  - **Runtime:** the mode resets to Normal on a session switch.
  - **Streaming:** a thinking block appended under Thinking renders open.
- [ ] **Step 2: Run** the focused vitest files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and budget. Expected: PASS. If the budget is over, the growth is
  load-bearing only if it is on first paint. Report it; don't raise the ceilings yourself.
- [ ] **Step 5: Commit** `feat(web): transcript view modes Normal/Thinking/Verbose, Ctrl/⌘+O (D-3)`

### Task 2: Message menu — Copy as Markdown

**Files:**
- Create: `web/src/features/sessions/transcript/MessageActions.tsx` (a `ui/Menu` trigger with items built from a list;
  the items for D-3 are just Copy as Markdown) and `web/src/session/turnMarkdown.ts` with
  `turnMarkdown(turn): string | null`.
- Modify: `Turn.tsx`, rendering `<MessageActions>` once per turn next to `TurnFeedback` on assistant turns and in the
  user lane for user turns. Reuse the repo's existing clipboard helper (`grep -rn "clipboard.writeText" web/src`)
  and the Sonner toast.
- Test: `turnMarkdown.test.ts`, `MessageActions.test.tsx`, `Turn.test.tsx`.

**Interfaces:**
- Produces:
  - `turnMarkdown(turn: Turn): string | null`, which returns `null` when there is nothing to copy.
  - `MessageActions({ turn })`

- [ ] **Step 1: Write failing tests**
  - **`turnMarkdown` assistant:** two text blocks around a tool group give `"a\n\nb"`.
  - **`turnMarkdown` user:** a user prompt gives its text.
  - **`turnMarkdown` null cases:** a tools-only assistant turn and an image-only user turn give `null`.
  - **Menu:** Copy as Markdown writes the exact string and shows the toast `Copied`. A rejected write shows
    `Couldn't copy`. The trigger is absent when `turnMarkdown` is null.
  - **Turn:** one `Message actions` trigger per turn.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates and budget. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): per-message menu with Copy as Markdown (D-3)`

### Task 3: Benchmark and docs

- Modify `scripts/desktop_parity_benchmark.py`:
  - `View modes Normal / Verbose / Thinking (Ctrl+O)` → `Y`, note `per session; Normal = Settings defaults`;
  - `Per-message copy as Markdown` → `Y`.
- Regenerate audit §3 from the script output. Add the two features to the product docs that describe the session
  transcript (grep `docs/product` for "transcript" or "tool detail").
- [ ] **Step 1: Run** `python3 scripts/desktop_parity_benchmark.py`. Expected: Target completion ≥ 70%.
- [ ] **Step 2:** Paste the output and update the docs. Commit `docs: D-3 benchmark rerun and transcript docs`.
