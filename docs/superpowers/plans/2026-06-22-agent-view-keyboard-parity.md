# Agent View Keyboard Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `←`/`Ctrl+Z` detach-to-list and rebind rename/close to `Ctrl+R`/`Ctrl+X`, matching Claude Code Agent View, across both responsive layouts.

**Architecture:** Frontend-only React/TS. Mobile uses a routed full-screen `SessionView` (already has a window keydown listener); desktop uses the two-pane `DesktopShell` (no in-session listener yet). The list-nav keys live in the shared `DispatchBar`. Changes are localized to four components + their tests.

**Tech Stack:** React 18, TypeScript, Vite, Tailwind, @testing-library/react + userEvent, vitest.

## Global Constraints

- Base branch: `feat/agent-view-keyboard-parity` off `origin/master` (already created; HEAD `bcce9da`). NEVER commit to `master`.
- TDD: write the failing test first, watch it fail, implement, watch it pass, commit (constitution Principle V).
- Test commands run from `web/`: `node_modules/.bin/vitest run <file>` (node_modules is a symlink to the canonical checkout — already wired). Full gate before PR: `node_modules/.bin/vitest run`, `node_modules/.bin/tsc --noEmit`, `node_modules/.bin/eslint src`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Empty-prompt gate = `document.activeElement` has `aria-label="Message"` (the Composer) and non-empty `value`. Detach is non-destructive (`detach()` keeps the session running).

---

### Task 1: Mobile `SessionView` — `←` and `Ctrl+Z` detach

**Files:**
- Modify: `web/src/components/session/SessionView.tsx` (the keydown `useEffect`, ~lines 63–92)
- Test: `web/src/components/session/__tests__/SessionView.keyboard.test.tsx`

**Interfaces:**
- Consumes: existing `handleBack` (`detach(sessionId); onBack()`), `hasOpenAction`, `onSetMode`, `toggleToolDetail`, `modeRef`.
- Produces: no new exports; behavior only.

- [ ] **Step 1: Write the failing tests** — append inside the `describe('SessionView keyboard shortcuts ...')` block:

```tsx
  it('ArrowLeft on an empty composer calls onBack', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /message/i }))
    await user.keyboard('{ArrowLeft}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('ArrowLeft does NOT call onBack when the composer has text', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ArrowLeft does NOT call onBack while a permission card is pending', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    await act(async () => {
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({ type: 'turnStart', turnId: 't1' }))
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({
        type: 'permissionRequest', permId: 'perm-1', turnId: 't1',
        tool: 'bash', toolKind: 'bash', input: { command: 'ls' }, plan: false,
      }))
    })
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('Ctrl+Z force-detaches even while a permission card is pending', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    await act(async () => {
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({ type: 'turnStart', turnId: 't1' }))
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({
        type: 'permissionRequest', permId: 'perm-1', turnId: 't1',
        tool: 'bash', toolKind: 'bash', input: { command: 'ls' }, plan: false,
      }))
    })
    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Ctrl+Z does NOT detach when the composer has text (undo preserved)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).not.toHaveBeenCalled()
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionView.keyboard.test.tsx`
Expected: the 3 new `ArrowLeft`/`Ctrl+Z`-that-should-detach tests FAIL (onBack not called); the 2 "does NOT" tests may pass by accident. The ArrowLeft/Ctrl+Z detach tests must fail.

- [ ] **Step 3: Implement** — replace the body of the `onKey` function in the keydown `useEffect` (`SessionView.tsx` ~63–88) with:

```tsx
    function onKey(e: KeyboardEvent) {
      const ae = document.activeElement as HTMLElement | null
      const typing = !!ae && (ae.tagName === 'TEXTAREA' || ae.tagName === 'INPUT' || ae.isContentEditable)
      if (!typing && (e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        toggleToolDetail()
        return
      }
      if (e.key === 'Tab' && e.shiftKey) {
        e.preventDefault()
        const next = nextPermissionMode(modeRef.current)
        onSetMode(next)
        return
      }
      // Composer-with-text guard, shared by ←/Esc (cursor move keeps the draft) and
      // Ctrl+Z (yield to textarea undo while editing).
      const ae2 = document.activeElement as HTMLTextAreaElement | null
      const inComposerWithText =
        ae2?.getAttribute('aria-label') === 'Message' && (ae2?.value ?? '') !== ''
      // Ctrl+Z → unconditional force-detach escape hatch (bypasses the pending-card guard).
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (inComposerWithText) return
        e.preventDefault()
        handleBack()
        return
      }
      // ← (empty prompt) and Esc → detach to the list. Shared guards: composer-with-text,
      // and a pending permission/question card (answer it first).
      if (e.key === 'ArrowLeft' || e.key === 'Escape') {
        if (inComposerWithText) return
        if (hasOpenAction) return
        e.preventDefault()
        handleBack()
      }
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionView.keyboard.test.tsx`
Expected: PASS (all, including the original 6).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/session/__tests__/SessionView.keyboard.test.tsx
git commit -m "feat(web): mobile SessionView left-arrow + Ctrl+Z detach to list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Desktop `DesktopShell` — `←`/`Esc`/`Ctrl+Z` detach-to-welcome

**Files:**
- Modify: `web/src/components/DesktopShell.tsx`
- Test: `web/src/components/__tests__/DesktopShell.test.tsx`

**Interfaces:**
- Consumes: `selection`, `setSelection`, `sessions`, `highlightedIndex`/`setHighlightedIndex`, `tab`, the `DispatchBar` `autoFocusKey` prop.
- Produces: `dispatchFocusNonce` state, `detachToList()` callback (internal).

- [ ] **Step 1: Write the failing tests** — append inside `describe('DesktopShell', ...)`:

```tsx
  it('ArrowLeft on an empty composer detaches to the welcome pane and highlights the origin row', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByText(/select a session from the left/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /new session prompt/i }))
      .toHaveAttribute('aria-activedescendant', 'row-s1')
  })

  it('Ctrl+Z detaches to the welcome pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    await user.keyboard('{Control>}z{/Control}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
  })

  it('ArrowLeft with composer text does NOT detach', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    const box = screen.getByRole('textbox', { name: /^message$/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node_modules/.bin/vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: the two detach tests FAIL (still showing the message composer); the "with text" test passes by accident.

- [ ] **Step 3a: Add state** — after the `highlightedIndex` declaration (`DesktopShell.tsx:140`) add:

```tsx
  const [dispatchFocusNonce, setDispatchFocusNonce] = useState(0)
```

- [ ] **Step 3b: Add `detachToList` + listener** — after `focusNew()` (`DesktopShell.tsx:176`) add:

```tsx
  const detachToList = useCallback(() => {
    const id = selection.type === 'session' ? selection.id : null
    setSelection({ type: 'new' })
    if (id) {
      const idx = sessions.findIndex((s) => s.id === id)
      if (idx >= 0) setHighlightedIndex(idx)
    }
    setDispatchFocusNonce((n) => n + 1) // return focus to DispatchBar for ↑/↓ nav
  }, [selection, sessions])

  // In-session detach gestures (desktop): ← / Esc / Ctrl+Z return to the list.
  // Active only while a session is focused. Guards: yield to the composer while it
  // holds a draft (cursor move / undo). Faithful detach → main pane shows WelcomePane.
  useEffect(() => {
    const active = tab === 'sessions' && selection.type === 'session'
    if (!active) return
    function onKey(e: KeyboardEvent) {
      const ae = document.activeElement as HTMLTextAreaElement | null
      const inComposerWithText =
        ae?.getAttribute('aria-label') === 'Message' && (ae?.value ?? '') !== ''
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
        return
      }
      if (e.key === 'ArrowLeft' || e.key === 'Escape') {
        if (inComposerWithText) return
        e.preventDefault()
        detachToList()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [tab, selection, detachToList])
```

- [ ] **Step 3c: Wire DispatchBar focus** — in the `DispatchBar` JSX (`DesktopShell.tsx:377–392`) add the prop:

```tsx
      autoFocusKey={dispatchFocusNonce}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node_modules/.bin/vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: PASS (all, including the existing DesktopShell tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopShell.test.tsx
git commit -m "feat(web): desktop DesktopShell left-arrow/Ctrl+Z detach to welcome

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Rebind rename/close to `Ctrl+R`/`Ctrl+X` in `DispatchBar`

**Files:**
- Modify: `web/src/components/DispatchBar.tsx` (handleKeyDown ~154–168; hint copy ~252; doc comments ~19,27)
- Test: `web/src/components/__tests__/DispatchBar.test.tsx`, `web/src/components/__tests__/KeyboardSessions.test.tsx`

**Interfaces:**
- Consumes: existing `onRowAction({type})`, `empty`, `confirmDeleteName`.
- Produces: behavior only — `Ctrl+R`→rename (empty), `Ctrl+X`→delete (empty); hint reads "Press Ctrl+X again to close".

- [ ] **Step 1: Update the DispatchBar unit tests** — replace the two `r` tests (`DispatchBar.test.tsx:184–203`) with:

```tsx
  it('Ctrl+R on an empty prompt triggers rename action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Control>}r{/Control}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'rename' })
  })

  it('Ctrl+R when the prompt has text does NOT rename and does not insert a char', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'fix ')
    await user.keyboard('{Control>}r{/Control}')
    expect(onRowAction).not.toHaveBeenCalled()
    expect(box).toHaveValue('fix ')
  })

  it('Ctrl+X on an empty prompt triggers delete action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Control>}x{/Control}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'delete' })
  })

  it('Ctrl+X when the prompt has text does NOT delete (native cut)', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'hi')
    await user.keyboard('{Control>}x{/Control}')
    expect(onRowAction).not.toHaveBeenCalled()
  })
```

- [ ] **Step 2: Update the Dashboard integration tests** in `KeyboardSessions.test.tsx`:
  - Line ~185: `'{ArrowDown}r'` → `'{ArrowDown}{Control>}r{/Control}'`.
  - Lines ~196, ~210, ~222, ~227: replace each `{Delete}` with `{Control>}x{/Control}` (the `{ArrowDown}{Delete}{Delete}` two-press becomes `{ArrowDown}{Control>}x{/Control}{Control>}x{/Control}`).
  - Line ~200: `/press delete again to close/i` → `/press ctrl\+x again to close/i`.
  - Rename the test titles referencing "Delete" to "Ctrl+X" for clarity (e.g. `'first Ctrl+X arms the guard ...'`).

- [ ] **Step 3: Run the tests to verify they fail**

Run: `node_modules/.bin/vitest run src/components/__tests__/DispatchBar.test.tsx src/components/__tests__/KeyboardSessions.test.tsx`
Expected: FAIL — the new `Ctrl+R`/`Ctrl+X` tests fail because the component still binds `r`/`F2`/`Delete`.

- [ ] **Step 4: Implement** — in `DispatchBar.tsx` replace the `r`/`F2` branch and the `Delete` branch (~154–168) with:

```tsx
    // Ctrl+R → rename highlighted row. preventDefault ALWAYS so the browser never
    // reloads and discards an in-progress prompt; act only when the prompt is empty.
    if (e.ctrlKey && (e.key === 'r' || e.key === 'R')) {
      e.preventDefault()
      if (empty) onRowAction({ type: 'rename' })
      return
    }

    // Ctrl+X → close (delete) highlighted row; two-press guard lives in the parent.
    // Only when the prompt is empty, so native "cut" still works while editing a draft.
    if (empty && e.ctrlKey && (e.key === 'x' || e.key === 'X')) {
      e.preventDefault()
      onRowAction({ type: 'delete' })
      return
    }
```

  Then update the hint copy (`DispatchBar.tsx:252`):

```tsx
          Press Ctrl+X again to close &lsquo;{confirmDeleteName}&rsquo;
```

  And the doc comments: line 19 `r / F2 / Delete / Alt+1..9` → `Ctrl+R / Ctrl+X / Alt+1..9`; line 27 `"press Delete again to close '<name>'"` → `"press Ctrl+X again to close '<name>'"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node_modules/.bin/vitest run src/components/__tests__/DispatchBar.test.tsx src/components/__tests__/KeyboardSessions.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.test.tsx web/src/components/__tests__/KeyboardSessions.test.tsx
git commit -m "feat(web): rebind rename/close to Ctrl+R/Ctrl+X in DispatchBar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Update the `?` shortcuts overlay

**Files:**
- Modify: `web/src/components/ShortcutsHelp.tsx` (`ACTION_BINDINGS` ~21, `IN_SESSION_BINDINGS` ~29)
- Test: `web/src/components/__tests__/ShortcutsHelp.test.tsx`

**Interfaces:** display-only copy.

- [ ] **Step 1: Write the failing test** — add to `ShortcutsHelp.test.tsx`:

```tsx
  it('documents the rebound and detach shortcuts', () => {
    render(<ShortcutsHelp onClose={() => {}} />)
    expect(screen.getByText('Ctrl+R')).toBeInTheDocument()
    expect(screen.getByText('Ctrl+X')).toBeInTheDocument()
    expect(screen.getByText('← / Esc')).toBeInTheDocument()
    expect(screen.getByText('Ctrl+Z')).toBeInTheDocument()
  })
```

  (Ensure `render`, `screen` from `@testing-library/react` and `ShortcutsHelp` are imported — mirror the existing test's imports.)

- [ ] **Step 2: Run to verify it fails**

Run: `node_modules/.bin/vitest run src/components/__tests__/ShortcutsHelp.test.tsx`
Expected: FAIL (`Ctrl+R` etc. not found).

- [ ] **Step 3: Implement** — update the binding arrays in `ShortcutsHelp.tsx`:

```tsx
const ACTION_BINDINGS: Binding[] = [
  { keys: 'Ctrl+R', description: 'Rename highlighted session' },
  { keys: 'Ctrl+X', description: 'Close highlighted session' },
  { keys: 'Enter (with text)', description: 'Create new session with prompt' },
  { keys: 'Shift+Enter', description: 'Insert newline in prompt' },
  { keys: '?', description: 'Show this shortcuts overlay' },
]

const IN_SESSION_BINDINGS: Binding[] = [
  { keys: '← / Esc', description: 'Detach to session list' },
  { keys: 'Ctrl+Z', description: 'Force-detach' },
  { keys: 'Shift+Tab', description: 'Cycle permission mode' },
  { keys: 'c', description: 'Collapse / expand tool calls' },
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `node_modules/.bin/vitest run src/components/__tests__/ShortcutsHelp.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/ShortcutsHelp.tsx web/src/components/__tests__/ShortcutsHelp.test.tsx
git commit -m "docs(web): update shortcuts overlay for Ctrl+R/Ctrl+X + detach keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full quality gate + PR

- [ ] **Step 1: Run the full web gate**

Run (from `web/`):
```bash
node_modules/.bin/vitest run && node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint src
```
Expected: all tests pass, no type errors, no lint errors. Fix any fallout before proceeding.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin feat/agent-view-keyboard-parity
gh pr create --base master --title "feat(web): Agent View keyboard parity (← / Ctrl+Z detach, Ctrl+R/Ctrl+X)" \
  --body "$(cat <<'EOF'
Adds Claude Code Agent View navigation parity across both responsive layouts:
- `←` / `Esc` (empty composer) and `Ctrl+Z` (force) detach a focused session back to the list — mobile `SessionView` and desktop `DesktopShell` (faithful detach-to-welcome, selecting the origin row).
- Rebinds rename/close to `Ctrl+R` / `Ctrl+X` in the shared `DispatchBar`.
- Updates the `?` shortcuts overlay.

Spec: docs/superpowers/specs/2026-06-22-agent-view-keyboard-parity-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (completed)

- **Spec coverage:** A→Task 1, B→Task 2, C→Task 3, D→Task 4. Mobile origin-row highlight is explicitly deferred in the spec (not a task). ✓
- **Placeholder scan:** none. ✓
- **Type consistency:** `detachToList`, `dispatchFocusNonce`, `inComposerWithText`, `onRowAction({type})` consistent across tasks; `autoFocusKey` is an existing `DispatchBar` prop. ✓
- **Known risk:** Task 2's listener reads `document.activeElement`; after focusing a session via row click, focus is on the row button (not the composer), so `←` detaches — matches intended behavior and the test asserts it.
