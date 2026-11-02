# Desktop-parity D-1: prompt row — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move session controls into a Desktop-style prompt row under the composer (mode, model, effort pickers and a
usage ring), make Esc stop a running turn, and bind keyboard menus for mode, model and effort.

**Architecture:** A new `PromptRow` renders under `Composer` inside `SessionComposer`, fed from the live
`SessionRuntime` and the session record. Its pickers reuse the existing runtime actions (`rt.onSetMode`,
`rt.onSetModel`). Effort rides the existing model-switch path, which gains an optional `effort` field. The controls
it replaces are deleted from `SessionMeta` in the same PR. Shortcuts are added only through `shell/shortcuts.ts`.

**Tech Stack:** React 19 + TS + Tailwind tokens (`web/`), vitest + Testing Library, Playwright e2e; FastAPI +
pytest (`server/`).

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §3, §3.1, §7 (keyboard table) and §11 row
D-1. Evidence and decisions: `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` (D9, D10, D14, D20).

## Global Constraints

- Mode **keys are unchanged** (`interactive`, `acceptEdits`, `plan`, `auto`, `bypassPermissions`, `allowlist`,
  `full-auto`). Only labels and grouping change. Stored settings and automations must not migrate.
- Labels are exact, in this order. Desktop group: **Ask** (`interactive`) · **Accept edits** (`acceptEdits`) ·
  **Plan** (`plan`) · **Auto** (`auto`) · **Bypass** (`bypassPermissions`). ViberPit group (heading "ViberPit"):
  **Read-only allowlist** (`allowlist`) · **Full auto** (`full-auto`).
- `web/src/session/permissionModes.ts` stays the single mode list. Settings › Defaults shows the same labels.
- Esc rule: while a turn runs, Esc stops it and shows the toast `Stopped · Esc`. While idle, Esc detaches exactly as
  today. ← always detaches.
- Keyboard menus use **Alt+Shift+M / I / E** (`⌥⇧M/I/E` on Apple), matched on `e.code` (`KeyM`, `KeyI`, `KeyE`),
  because Ctrl+Shift+I (DevTools) and Ctrl/⌘+Shift+M (Chrome profile menu) are browser-reserved. The spec's ⌘⇧ chord
  is the documented fallback case.
- Effort is **capability-gated**: the picker renders only when `can(caps, 'reasoningEffort')` and the chosen model
  lists `reasoningEfforts`. Claude effort is a separate PR (needs verifying against the CLI binary first; spec §1.5).
- Every new action is **hidden, not disabled**, on an engine lacking the capability.
- No `func.now()`, no hand-rolled cancel (repo rules). Web gates: `npm run typecheck`, `npm run lint`
  (`--max-warnings 0`), full `npm run test`, `npm run build`. Server gates: ruff, mypy, `pytest server/tests/`.

## Review Focus

1. **Esc while the slash-command palette, mention menu or a dialog is open:** it must close that overlay and must
   not stop the turn. Pinned in Task 4.
2. **Esc while a permission or question card is pending and the session is busy:** stopping would abandon the ask
   silently, so Esc must not stop. Pinned in Task 4.
3. **Switching from Plan to a non-default mode:** choosing Full auto while in Plan must land in Full auto, not the
   pre-plan mode. Pinned in Task 2 (server).
4. **A Codex session:** the picker must show only modes the engine accepts, and the effort picker must appear only
   where the model lists efforts. Pinned in Task 2 and Task 3.
5. **A model switch while busy:** the existing "after this turn" / "now" dialog and the gated-model approval flow
   must survive the move out of `SessionMeta`. Pinned in Task 3.

---

### Task 1: Desktop labels and groups on the single mode list

**Files:**
- Modify: `web/src/session/permissionModes.ts`
- Modify: `web/src/features/settings/sections/SessionDefaultsSection.tsx` (render groups via `<optgroup>`)
- Test: `web/src/session/permissionModes.test.ts` (create), plus update any test or `web/e2e` spec asserting old
  labels (`grep -rn "Ask each time\|Auto-approve safe tools\|Full auto — incl" web/src web/e2e`)

**Interfaces:**
- Produces:
  - `PERMISSION_MODES: readonly { value: PermissionMode; label: string; group: 'desktop' | 'viberpit' }[]`, the
    settable defaults with the same members as today (no `plan`; the server PUT set must still equal it, see
    `api/hooks.ts:393`).
  - `SESSION_MODE_OPTIONS: readonly { value: SessionMode; label: string; group: 'desktop' | 'viberpit' }[]`,
    derived from `PERMISSION_MODES` plus `{ value: 'plan', label: 'Plan', group: 'desktop' }`, in the Global
    Constraints order.
  - `type SessionMode = PermissionMode | 'plan'`
  - `modeLabel(mode: string): string`, which returns the label, or the raw key for an unknown mode.

- [ ] **Step 1: Write failing tests** `permissionModes.test.ts`:
  - `SESSION_MODE_OPTIONS.map(o => o.label)` equals
    `['Ask','Accept edits','Plan','Auto','Bypass','Read-only allowlist','Full auto']`.
  - Every `PERMISSION_MODES` value is in `SESSION_MODE_OPTIONS`.
  - `PERMISSION_MODES` has no `plan`.
  - `modeLabel('full-auto') === 'Full auto'`.
  - `modeLabel('default') === 'Ask'`, because the SDK sentinel for plan-exit reads as Ask.
  - `modeLabel('weird') === 'weird'`.
- [ ] **Step 2: Run** `(cd web && npx vitest run src/session/permissionModes.test.ts)`. Expected: FAIL.
- [ ] **Step 3: Implement** the list changes and the `<optgroup>` rendering in Settings.
- [ ] **Step 4: Run** the full `(cd web && npm run test)` and fix label assertions it surfaces. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): Desktop permission-mode labels and groups (D-1)`

### Task 2: Mode picker in the prompt row (Plan toggle removed)

**Files:**
- Create: `web/src/features/sessions/composer/PromptRow.tsx`, `web/src/features/sessions/composer/ModePicker.tsx`
- Modify: `web/src/features/sessions/composer/SessionComposer.tsx`: render `<PromptRow>` under `<Composer>`; rename
  the dead `hideModeSelector` prop to `hideSessionControls` (the assistant drawer passes it; it hides the whole row).
  Update the `AssistantSheet` call site.
- Modify: `web/src/features/sessions/SessionMeta.tsx`: delete the Plan toggle and its `pendingMode` status
  (`:331-349`) and the `onSetMode`/`mode`/`pendingMode` props; update `SessionBody.tsx:109,127`.
- Test: `web/src/features/sessions/composer/ModePicker.test.tsx`, `PromptRow.test.tsx`;
  `server/tests/test_permission_mode_switch.py` (create, or extend the existing driver mode test if
  `grep -rln "_pre_plan_mode" server/tests` finds one)

**Interfaces:**
- Consumes: `SESSION_MODE_OPTIONS`, `modeLabel` (Task 1); `rt.state` mode and `rt.pendingMode`, `rt.onSetMode(mode)`
  (`session/useSessionRuntime.ts:104,447`).
- Produces:
  - `PromptRow({ rt, session, caps, menuRef })`, where `menuRef` is
    `React.RefObject<PromptRowMenus>`.
  - `interface PromptRowMenus { openMode(): void; openModel(): void; openEffort(): void }`, used by Task 5.
  - `ModePicker({ mode, pendingMode, options, onSelect, open?, onOpenChange? })` built on the existing `ui/Menu`
    primitive.

- [ ] **Step 1: Write failing tests:**
  - **Labels:** ModePicker shows the current mode label (`Plan` when mode is `plan`; `Ask` when `default`).
  - **Menu:** opening lists two groups with the exact labels; choosing **Full auto** calls `onSelect('full-auto')`.
  - **Pending:** with `pendingMode` set it shows `Mode change pending` (role `status`).
  - **Assistant drawer:** PromptRow with `hideSessionControls` renders nothing.
  - **Codex:** for an engine whose capabilities restrict modes, only the allowed modes are listed. Find the
    capability first: `grep -n "permission\|mode" web/src/session/engineCapabilities.ts`. If no restriction exists,
    assert all seven are listed and note it in the commit body.
  - **SessionMeta:** no element named `Plan mode` remains (update `SessionMeta` tests).
  - **Server:** a driver in `plan` (entered from `auto`) receiving `set_permission_mode('full-auto')` ends with
    `mode == 'full-auto'`; receiving `'default'` restores `'auto'`.
- [ ] **Step 2: Run** `(cd web && npx vitest run src/features/sessions)` and
  `pytest server/tests/test_permission_mode_switch.py`. Expected: the new tests FAIL. The server test may already
  pass, since `claude_driver.py:2100-2113` handles it; keep it as a regression pin.
- [ ] **Step 3: Implement** PromptRow and ModePicker, wire SessionComposer, and delete the SessionMeta toggle.
- [ ] **Step 4: Run** the full `npm run test` + `npm run typecheck` + `npm run lint`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): in-session mode picker in the prompt row (D-1)`

### Task 3: Model and effort pickers move into the prompt row

**Files:**
- Create: `web/src/features/sessions/composer/ModelPicker.tsx`, `EffortPicker.tsx`
- Modify: `web/src/features/sessions/SessionMeta.tsx`: delete the model `<select>` block (`:198-237`) and the effort
  readout (`:239-251`). Keep the live-model and pending readouts by moving them into ModelPicker.
- Modify: `web/src/features/sessions/SessionBody.tsx`: the `ModelSwitchDialog` / `pendingModelId` flow
  (`:71,124,312-318`) is triggered from ModelPicker's `onSelect` instead of SessionMeta.
- Modify (server): `server/api/sessions.py` `SetModelRequest` gains `effort: str | None = None`, threaded through
  `session_actions.set_model(..., effort=...)` into the orchestrator's existing `reasoning_effort` argument of
  `set_model_for_session`. The argument is passed **only when present** (`EFFORT_UNCHANGED` otherwise, see
  `server/sessions/engine.py:26-30`). Mirror it in the WS/remote path if `session_actions.set_model` is reached over
  RPC (`grep -n "set_model" server/ipc/*.py server/sessions/fleet_orchestrator.py`).
- Modify: `web/src/api/hooks.ts` and `session/useSessionRuntime.ts`: `onSetModel(id, when, effort?)`.
- Test: `ModelPicker.test.tsx`, `EffortPicker.test.tsx`; `server/tests/test_set_model_effort.py`

**Interfaces:**
- Consumes: `PromptRowMenus` (Task 2); `can(caps, 'reasoningEffort')`; model catalog entries' `reasoningEfforts` and
  `defaultReasoningEffort` (`api/types.ts:649-652`).
- Produces: `ModelPicker({ chosen, live, pending, models, onSelect })` and
  `EffortPicker({ effort, efforts, onSelect })`; both expose `open()` through the forwarded menu handle.

- [ ] **Step 1: Write failing tests:**
  - **ModelPicker:** shows the chosen alias, never the SDK id; selecting a model while the session is busy opens
    `ModelSwitchDialog`; a pending switch shows `after this turn`.
  - **EffortPicker:** absent when the capability is false, and absent when the model has no `reasoningEfforts`;
    selecting `high` calls `onSetModel(currentModel, 'now', 'high')`.
  - **Server, effort sent:** `POST /sessions/{id}/model` `{"model":"gpt-5","effort":"high"}` reaches
    `set_model_for_session` with `reasoning_effort="high"`.
  - **Server, effort omitted:** without `effort` it passes `EFFORT_UNCHANGED`.
  - **Server, Claude:** for a Claude session with `effort`, the response is 4xx with code
    `unsupported_reasoning_effort` (reuse the code at `server/api/sessions.py:828`).
- [ ] **Step 2: Run** the new web tests + `pytest server/tests/test_set_model_effort.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the web gates and `pytest server/tests/test_set_model_effort.py server/tests/ -k "model"`;
  also `ruff check server/` and `mypy server/ --ignore-missing-imports`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: model and effort pickers in the prompt row (D-1)`

### Task 4: Usage ring and Esc-stops

**Files:**
- Modify: `PromptRow.tsx`: render `ui/UsageRing` with `value = (100 - contextPct) / 100` and the label
  `Context ${100 - contextPct}% used`, with ` · auto-compacts soon` appended when `contextSeverity(contextPct)` is
  `'danger'` (`lib/contextWindow.ts`). It renders nothing when `contextPct == null`. Delete the `ctx` readout from
  `SessionMeta.tsx:281-297`; the quota readout stays until D-6.
- Modify: `web/src/shell/shortcuts.ts`:
  - Add `sessionBusy: boolean` to `ShortcutContext`.
  - Add the row `insession.stop` (keys `Esc`, description `Stop the running turn`, group `in-session`) **before**
    `insession.detach`, matched on `plain(e.key === 'Escape')`, with
    `when: c.sessionOpen && c.sessionBusy && !c.overlayOpen && !c.hasOpenAction`.
  - Narrow `insession.detach`'s `when` with `!c.sessionBusy` **for Escape only**. ← keeps detaching while busy, so
    split the row: `insession.detach` (←, unchanged guard) and `insession.detachEsc` (Esc, idle only).
- Modify: the context builders (`MainPaneSession.tsx:96`, `AppShell.tsx:320`, `sessionGestures.tsx:33`) to supply
  `sessionBusy` from the runtime status (busy = the same predicate the Stop button uses in `Composer.tsx:630-638`).
  The stop handler calls `rt.onInterrupt()` and `toast('Stopped · Esc')` (Sonner, `shell/AppToaster.tsx`).
- Test: extend `web/src/shell/useShortcuts.test.tsx` (the `Esc / ← / Ctrl+Z detach` block at `:391`) and
  `PromptRow.test.tsx`

- [ ] **Step 1: Write failing tests:**
  - **Busy:** Esc calls `interrupt` and does not detach, and the toast shows `Stopped · Esc`.
  - **Idle:** Esc detaches (existing behaviour).
  - **Busy + ←:** detaches.
  - **Busy + slash palette open:** Esc closes the palette, no interrupt.
  - **Busy + pending permission card:** Esc neither interrupts nor detaches.
  - **Busy + composer has a draft:** Esc interrupts. Desktop stops regardless of the draft, and the draft is kept.
  - **Usage ring:** label at `contextPct=20` is `Context 80% used`; at the danger tier it ends with
    `auto-compacts soon`; absent when `contextPct` is null.
- [ ] **Step 2: Run** `(cd web && npx vitest run src/shell/useShortcuts.test.tsx src/features/sessions/composer)`.
  Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates. Expected: PASS. The help sheet lists `Stop the running turn` automatically;
  the registry test, which checks that every row has a handler, must stay green.
- [ ] **Step 5: Commit** `feat(web): Esc stops a running turn; usage ring in the prompt row (D-1)`

### Task 5: Keyboard menus Alt+Shift+M / I / E

**Files:**
- Modify: `web/src/shell/shortcuts.ts`: rows `insession.modeMenu` / `insession.modelMenu` / `insession.effortMenu`,
  keys `Alt+Shift+M` / `Alt+Shift+I` / `Alt+Shift+E` (`keysMac` `⌥⇧M` / `⌥⇧I` / `⌥⇧E`), matched on
  `e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyM' | 'KeyI' | 'KeyE'`, with
  `when: c.sessionOpen && c.interactive && !c.overlayOpen`. They fire while typing; a chord is not a character.
  Extend the "NOT BOUND, and why" comment with the Ctrl+Shift+I / ⌘⇧M reservation.
- Modify: the dispatcher wiring that owns the in-session handlers, calling `menuRef.current?.openMode()` and so on
  (Task 2's `PromptRowMenus`). The effort row is a no-op when EffortPicker is hidden.
- Test: `web/src/shell/useShortcuts.test.tsx`; `web/e2e/` add `prompt-row.spec.ts`

- [ ] **Step 1: Write failing tests:**
  - **Opens menus:** Alt+Shift+M opens the mode menu (role `menu` with `Ask`) with the composer focused;
    Alt+Shift+I opens the model menu.
  - **Hidden effort:** Alt+Shift+E does nothing on a Claude session (no effort picker).
  - **E2E:** the chord's keydown reaches the page in Chromium (`page.keyboard.press('Alt+Shift+KeyM')` opens the
    menu).
  - **E2E:** the prompt row renders at 390, 820 and 1440px with `scrollWidth <= clientWidth`.
- [ ] **Step 2: Run** vitest + `(cd web && npx playwright test e2e/prompt-row.spec.ts)`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates + the new e2e spec, plus
  `grep -rn "Plan mode\|Select model" web/e2e`, updating any spec still pointing at the removed SessionMeta
  controls. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): keyboard menus for mode, model and effort (D-1)`

### Task 6: Benchmark and docs

**Files:**
- Modify: `scripts/desktop_parity_benchmark.py`, flipping the statuses of these rows:
  - `Stop button + Esc stops the turn` → `Y`
  - `Model picker in prompt row` → `Y`
  - `Permission-mode picker (5 modes) in session` → `Y`
  - `Usage ring in prompt row` → `Y`
  - `Keyboard menus for mode / model / effort` → `Y`
  - `Effort picker in session` → stays `P`, with the note `Codex only; Claude effort pending CLI verification`
- Modify: `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` §3 (paste the script output) and
  `docs/product/features/*` pages describing the meta strip's model/plan controls
  (`grep -rln "plan toggle\|Plan mode toggle\|model switcher" docs/product`).

- [ ] **Step 1: Run** `python3 scripts/desktop_parity_benchmark.py`. Expected: Overall > 61%; Target completion ≥ 67%.
- [ ] **Step 2: Paste** the output into the audit §3 and update the product docs.
- [ ] **Step 3: Commit** `docs: D-1 benchmark rerun and prompt-row docs`
