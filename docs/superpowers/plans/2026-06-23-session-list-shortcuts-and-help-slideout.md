# Session-list Shortcuts + Global Help Slide-out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rationalize the session-list keyboard shortcuts onto a conflict-free `Alt` family driven by a single shortcuts registry, and replace the shortcuts modal with a global help slide-out that documents shortcuts, navigation, and core concepts.

**Architecture:** One registry module (`web/src/lib/shortcuts.ts`) is the single source of truth. The shared `DispatchBar` component matches `Alt` combos against it and dispatches via the existing `onRowAction` prop; the new `HelpDrawer` renders from it. Both `Dashboard` and `DesktopShell` already render `DispatchBar`, so matching is shared automatically — no second handler. The old hardcoded `Ctrl+R`/`Ctrl+X` branches and the `ShortcutsHelp` modal are deleted in the same change.

**Tech Stack:** React 18 + TypeScript + Vite, Tailwind, `lucide-react` icons, Vitest + Testing Library + `@testing-library/user-event`. No new dependencies.

## Global Constraints

- **Run all web commands from `web/`.** Tests: `npx vitest run <file>`. Type gate: `npx tsc -b` (NOT `tsc --noEmit` — it checks nothing here). Lint: `npx eslint <files>` (CI uses `--max-warnings 0` and the `react-hooks` rule).
- **Use the deploy-checkout `node_modules`.** A fresh worktree's `web/node_modules` may be incomplete; if `vitest` is missing, symlink it from the main checkout and run the local `.bin`, not `npx`-over-network.
- **CI runs the FULL vitest suite**, so a changed shared type can break sibling tests — keep the `onRowAction` union change consistent everywhere it is referenced.
- **Match existing patterns:** dialogs are hand-built fixed overlays (`role="dialog"`, `aria-modal`, Esc-to-close, focus trap). No shadcn Sheet/Drawer dependency.
- **Shortcut matching uses `KeyboardEvent.code`** (e.g. `'KeyC'`) for the `Alt`+letter combos, NOT `e.key` — on macOS `Option`+letter remaps `e.key` to a special char (ç, ®), but `e.code` stays layout-stable.
- **The handler MUST call `preventDefault()` on every matched `Alt` binding** (`Alt+D` focuses the browser address bar otherwise).
- Final scheme (verbatim): `Alt+C` continue · `Alt+R` rename · `Alt+A` archive (press-twice guard) · `Alt+D` delete (opens confirm dialog). `Ctrl+R` and `Ctrl+X` are removed. `↑/↓`, `Enter`/`→`, `Esc`, `?`, `Alt+1-9` unchanged.

---

### Task 1: Shortcuts registry (single source of truth)

**Files:**
- Create: `web/src/lib/shortcuts.ts`
- Test: `web/src/lib/shortcuts.test.ts`

**Interfaces:**
- Produces:
  - `type SessionRowAction = { type: 'rename' } | { type: 'archive' } | { type: 'permanent-delete' } | { type: 'continue' }`
  - `type RowAction = SessionRowAction | { type: 'jump'; index: number }`
  - `type KeyboardLike = Pick<KeyboardEvent, 'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>`
  - `type ShortcutGroup = 'sessions-list' | 'in-session'`
  - `interface Shortcut { id: string; keys: string; description: string; group: ShortcutGroup; action?: SessionRowAction; match?: (e: KeyboardLike) => boolean }`
  - `const SHORTCUTS: Shortcut[]`
  - `function matchSessionShortcut(e: KeyboardLike): Shortcut | undefined`
  - `function shortcutsByGroup(group: ShortcutGroup): Shortcut[]`

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/shortcuts.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { SHORTCUTS, matchSessionShortcut, shortcutsByGroup, type KeyboardLike } from './shortcuts'

function ev(over: Partial<KeyboardLike>): KeyboardLike {
  return { key: '', code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...over }
}

describe('shortcuts registry', () => {
  it('matches each Alt action by physical code, regardless of e.key', () => {
    // macOS Option+C remaps e.key to 'ç' — match must rely on code, not key
    expect(matchSessionShortcut(ev({ code: 'KeyC', key: 'ç', altKey: true }))?.action).toEqual({ type: 'continue' })
    expect(matchSessionShortcut(ev({ code: 'KeyR', altKey: true }))?.action).toEqual({ type: 'rename' })
    expect(matchSessionShortcut(ev({ code: 'KeyA', altKey: true }))?.action).toEqual({ type: 'archive' })
    expect(matchSessionShortcut(ev({ code: 'KeyD', altKey: true }))?.action).toEqual({ type: 'permanent-delete' })
  })

  it('does NOT match when Alt is absent or Ctrl/Meta is also held', () => {
    expect(matchSessionShortcut(ev({ code: 'KeyC', key: 'c' }))).toBeUndefined()
    expect(matchSessionShortcut(ev({ code: 'KeyA', altKey: true, ctrlKey: true }))).toBeUndefined()
    expect(matchSessionShortcut(ev({ code: 'KeyD', altKey: true, metaKey: true }))).toBeUndefined()
  })

  it('every wired shortcut has a display string and lives in sessions-list', () => {
    for (const s of SHORTCUTS.filter((x) => x.match)) {
      expect(s.keys).toMatch(/\S/)
      expect(s.group).toBe('sessions-list')
      expect(s.action).toBeDefined()
    }
  })

  it('exposes in-session display entries for the help panel', () => {
    expect(shortcutsByGroup('in-session').length).toBeGreaterThan(0)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/lib/shortcuts.test.ts`
Expected: FAIL — `Cannot find module './shortcuts'`.

- [ ] **Step 3: Write the registry**

Create `web/src/lib/shortcuts.ts`:

```ts
/** A row action dispatched against the currently-highlighted session. */
export type SessionRowAction =
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'continue' }

/** Full union accepted by DispatchBar's onRowAction (adds parameterized jump). */
export type RowAction = SessionRowAction | { type: 'jump'; index: number }

/** Minimal shape satisfied by both a DOM KeyboardEvent and a React.KeyboardEvent. */
export type KeyboardLike = Pick<
  KeyboardEvent,
  'key' | 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'
>

export type ShortcutGroup = 'sessions-list' | 'in-session'

export interface Shortcut {
  id: string
  keys: string // display string, e.g. 'Alt+C'
  description: string
  group: ShortcutGroup
  /** Present ⇒ this shortcut is wired into the DispatchBar handler. */
  action?: SessionRowAction
  match?: (e: KeyboardLike) => boolean
}

/** Alt + a physical letter key, ignoring keyboard layout (macOS Option remaps e.key). */
function altCode(code: string) {
  return (e: KeyboardLike): boolean =>
    e.altKey && !e.ctrlKey && !e.metaKey && e.code === code
}

export const SHORTCUTS: Shortcut[] = [
  // ── Sessions list — WIRED (Alt actions) ──────────────────────────────
  { id: 'session.continue', keys: 'Alt+C', description: 'Continue (resume) highlighted session',
    group: 'sessions-list', action: { type: 'continue' }, match: altCode('KeyC') },
  { id: 'session.rename', keys: 'Alt+R', description: 'Rename highlighted session',
    group: 'sessions-list', action: { type: 'rename' }, match: altCode('KeyR') },
  { id: 'session.archive', keys: 'Alt+A', description: 'Archive highlighted session (press twice)',
    group: 'sessions-list', action: { type: 'archive' }, match: altCode('KeyA') },
  { id: 'session.delete', keys: 'Alt+D', description: 'Delete highlighted session permanently',
    group: 'sessions-list', action: { type: 'permanent-delete' }, match: altCode('KeyD') },
  // ── Sessions list — DISPLAY-ONLY (bespoke handling stays in DispatchBar) ──
  { id: 'session.nav', keys: '↑ / ↓', description: 'Move highlight up / down', group: 'sessions-list' },
  { id: 'session.open', keys: '→ / Enter', description: 'Open highlighted session (empty prompt)', group: 'sessions-list' },
  { id: 'session.jump', keys: 'Alt+1–9', description: 'Open session by number', group: 'sessions-list' },
  { id: 'session.create', keys: 'Enter', description: 'Create new session from typed prompt', group: 'sessions-list' },
  { id: 'session.newline', keys: 'Shift+Enter', description: 'Insert a newline in the prompt', group: 'sessions-list' },
  { id: 'session.deselect', keys: 'Esc', description: 'Clear the prompt and deselect', group: 'sessions-list' },
  { id: 'session.help', keys: '?', description: 'Open this help panel', group: 'sessions-list' },
  // ── In-session — DISPLAY-ONLY ────────────────────────────────────────
  { id: 'insession.detach', keys: '← / Esc', description: 'Detach back to the session list', group: 'in-session' },
  { id: 'insession.forceDetach', keys: 'Ctrl+Z', description: 'Force-detach to the list', group: 'in-session' },
  { id: 'insession.permMode', keys: 'Shift+Tab', description: 'Cycle permission mode', group: 'in-session' },
]

export function matchSessionShortcut(e: KeyboardLike): Shortcut | undefined {
  return SHORTCUTS.find((s) => s.match?.(e))
}

export function shortcutsByGroup(group: ShortcutGroup): Shortcut[] {
  return SHORTCUTS.filter((s) => s.group === group)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/lib/shortcuts.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck + commit**

Run: `npx tsc -b`
Expected: no errors.

```bash
git add web/src/lib/shortcuts.ts web/src/lib/shortcuts.test.ts
git commit -m "feat(web): add shortcuts registry as single source of truth"
```

---

### Task 2: Wire DispatchBar to the registry (Alt scheme, remove Ctrl+R/Ctrl+X)

**Files:**
- Modify: `web/src/components/DispatchBar.tsx` (prop type ~22-24; handler ~157-171; hint text ~254)
- Test: `web/src/components/__tests__/DispatchBar.test.tsx` (replace the Ctrl tests ~184-222)

**Interfaces:**
- Consumes: `matchSessionShortcut`, `RowAction` from `@/lib/shortcuts` (Task 1).
- Produces: `DispatchBar`'s `onRowAction` prop now typed `(action: RowAction) => void`; it fires `{type:'continue'|'rename'|'archive'|'permanent-delete'}` for `Alt+C/R/A/D` and `{type:'jump';index}` for `Alt+1-9`.

- [ ] **Step 1: Replace the Ctrl tests with Alt tests (failing)**

In `web/src/components/__tests__/DispatchBar.test.tsx`, delete the four tests titled `Ctrl+R …` and `Ctrl+X …` (lines ~184-222) and add:

```ts
  it('Alt+R triggers the rename action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}r{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'rename' })
  })

  it('Alt+A triggers the archive action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}a{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'archive' })
  })

  it('Alt+C triggers the continue action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}c{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'continue' })
  })

  it('Alt+D triggers the permanent-delete action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}d{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'permanent-delete' })
  })

  it('Alt actions fire even with a draft, and insert no character', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'hello')
    await user.keyboard('{Alt>}a{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'archive' })
    expect(box).toHaveValue('hello')
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: FAIL — the new Alt tests fail (handler still fires `{type:'delete'}` on `Ctrl+X` and ignores `Alt` letters).

- [ ] **Step 3: Update the prop type**

In `DispatchBar.tsx`, replace the `onRowAction` prop type (lines ~21-24):

```tsx
  /** Alt+C/R/A/D and Alt+1..9 on the highlighted row. */
  onRowAction: (action: RowAction) => void
```

Add the import at the top:

```tsx
import { matchSessionShortcut, type RowAction } from '@/lib/shortcuts'
```

- [ ] **Step 4: Replace the Ctrl branches with registry matching**

In `handleKeyDown`, **delete** the `Ctrl+R` block (lines ~157-163) and the `Ctrl+X` block (lines ~165-171). In their place, after the `Alt+Digit` block (which stays), add:

```tsx
    // Alt+letter session actions (continue/rename/archive/delete) — matched from the
    // shortcuts registry by physical key code so macOS Option-remapping is irrelevant.
    // preventDefault ALWAYS so Alt+D never focuses the browser address bar, etc.
    const sc = matchSessionShortcut(e)
    if (sc?.action) {
      e.preventDefault()
      onRowAction(sc.action)
      return
    }
```

- [ ] **Step 5: Update the press-twice hint text**

In `DispatchBar.tsx` (line ~254), change the hint from `Ctrl+X` to `Alt+A`:

```tsx
          Press Alt+A again to close &lsquo;{confirmDeleteName}&rsquo;
```

- [ ] **Step 6: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: PASS (Alt action tests pass; Alt+2 jump test still passes).

- [ ] **Step 7: Typecheck, lint, commit**

Run: `npx tsc -b && npx eslint src/components/DispatchBar.tsx src/components/__tests__/DispatchBar.test.tsx`
Expected: no errors (note: `tsc -b` will flag `handleRowAction` callers in Dashboard/DesktopShell once the `'delete'` member is gone — those are fixed in Tasks 3 & 4; if `tsc -b` errors only there, proceed and let those tasks resolve them, OR run Tasks 2-4 as one commit). To keep the build green per-commit, run Tasks 3 and 4 before the `tsc -b` gate, then commit together.

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.test.tsx
git commit -m "feat(web): drive DispatchBar shortcuts from registry (Alt scheme)"
```

> **Note for the implementer:** renaming the `'delete'` union member to `'archive'` makes the two `handleRowAction` callers (Dashboard, DesktopShell) type-error until Tasks 3 & 4 land. If your review process requires every commit to typecheck, implement Tasks 2-4 together and run a single `tsc -b` before committing.

---

### Task 3: Extend `handleRowAction` in Dashboard (archive/continue/permanent-delete)

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (`handleRowAction` ~1485-1530)
- Test: `web/src/components/__tests__/KeyboardSessions.test.tsx` (update Ctrl tests ~219-266; add Alt+C / Alt+D tests)

**Interfaces:**
- Consumes: `RowAction` from `@/lib/shortcuts`; existing `handleQuickResume(id, text?)` (~1427), `handleSessionDelete(id)` (~1450), `handleClose(id)` (~1445), `pendingDelete` guard.
- Produces: Dashboard handles all four `RowAction` types.

- [ ] **Step 1: Update the integration tests (failing)**

In `KeyboardSessions.test.tsx`:
- Rename test `Ctrl+R on empty prompt opens the rename dialog…` → use `{Alt>}r{/Alt}`:

```ts
    await user.keyboard('{ArrowDown}{Alt>}r{/Alt}')
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
```

- In the three `Ctrl+X` guard tests (~228-266), replace every `{Control>}x{/Control}` with `{Alt>}a{/Alt}`. Logic and assertions are otherwise unchanged.
- Add two new tests (place after the guard tests). These rely on the file's existing hook-mock setup; mirror how `useCloseSession` is asserted in the guard tests for the `sendPrompt`/`permanentDelete` mocks:

```ts
  it('Alt+C sends "continue" to the highlighted session', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useSendPrompt).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useSendPrompt>)
    const user = userEvent.setup()
    renderDashboard() // use the file's existing render helper
    await user.keyboard('{ArrowDown}{Alt>}c{/Alt}')
    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ text: 'continue' }))
  })

  it('Alt+D opens the permanent-delete confirmation dialog', async () => {
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Alt>}d{/Alt}')
    expect(screen.getByRole('dialog', { name: /delete/i })).toBeInTheDocument()
  })
```

> Adjust `renderDashboard()` / `NOOP_MUTATION` / the `useSendPrompt` mock to the exact names already used in this test file (it already mocks `@/api/hooks`). If `useSendPrompt` is not yet in the mock object, add it alongside the existing mocked hooks.

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/KeyboardSessions.test.tsx`
Expected: FAIL — `Alt+R`/`Alt+A`/`Alt+C`/`Alt+D` not handled; rename dialog/continue/delete assertions fail.

- [ ] **Step 3: Extend `handleRowAction`**

In `Dashboard.tsx`, change the union annotation and the `'delete'` branch to `'archive'`, and add `'continue'` and `'permanent-delete'` branches. Replace the function header + branches:

```tsx
  function handleRowAction(action: RowAction) {
    if (action.type === 'rename') {
      clearPendingDelete()
      if (selectedSession) handleRename(selectedSession.id)
    } else if (action.type === 'continue') {
      clearPendingDelete()
      if (selectedSession) handleQuickResume(selectedSession.id)
    } else if (action.type === 'permanent-delete') {
      clearPendingDelete()
      if (selectedSession) handleSessionDelete(selectedSession.id)
    } else if (action.type === 'archive') {
      if (!selectedSession) return
      const id = selectedSession.id
      const now = Date.now()
      if (pendingDelete && pendingDelete.id === id && now - pendingDelete.ts < 2000) {
        clearPendingDelete()
        handleClose(id)
      } else {
        if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
        setPendingDelete({ id, ts: now })
        pendingDeleteTimeoutRef.current = setTimeout(() => {
          setPendingDelete(null)
          pendingDeleteTimeoutRef.current = null
        }, 2000)
      }
    } else if (action.type === 'jump') {
      clearPendingDelete()
      const s = orderedSessions[action.index]
      if (s) {
        setHighlightedIndex(action.index)
        handleOpen(s.id)
      }
    }
  }
```

Add the import:

```tsx
import type { RowAction } from '@/lib/shortcuts'
```

> Note: the original `rename`/`jump` branches inlined the pending-delete clearing; this uses the existing `clearPendingDelete()` helper (defined ~1532) for DRY. Behavior is identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/KeyboardSessions.test.tsx`
Expected: PASS.

- [ ] **Step 5: Typecheck + commit**

Run: `npx tsc -b`
Expected: no errors in `Dashboard.tsx` (DesktopShell may still error until Task 4).

```bash
git add web/src/components/Dashboard.tsx web/src/components/__tests__/KeyboardSessions.test.tsx
git commit -m "feat(web): Alt+C/R/A/D session actions in Dashboard"
```

---

### Task 4: Extend `handleRowAction` in DesktopShell (mirror Task 3)

**Files:**
- Modify: `web/src/components/DesktopShell.tsx` (`handleRowAction` ~269-298)
- Test: `web/src/components/__tests__/DesktopShell.test.tsx`

**Interfaces:**
- Consumes: `RowAction`; existing `handleQuickResume` (~220), `setSessionDeleteTarget` (~123/238), `handleClose`, `highlighted`, `pendingDelete` guard, `orderedSessions`, `focusSession`.
- Produces: DesktopShell handles all four `RowAction` types identically to Dashboard.

- [ ] **Step 1: Write/adjust a failing test**

In `DesktopShell.test.tsx`, add a test asserting `Alt+A` arms the archive guard (mirror the Dashboard guard test, using this file's render helper + `useCloseSession` mock). If the file has no keyboard test yet, add:

```ts
  it('Alt+A arms the archive guard (does not close on first press)', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useCloseSession>)
    const user = userEvent.setup()
    renderDesktopShell() // file's existing helper
    await user.keyboard('{ArrowDown}{Alt>}a{/Alt}')
    expect(mutate).not.toHaveBeenCalled()
    await user.keyboard('{Alt>}a{/Alt}')
    expect(mutate).toHaveBeenCalled()
  })
```

> Use the names already present in `DesktopShell.test.tsx`. If it lacks a hook-mock harness, copy the mock pattern from `KeyboardSessions.test.tsx`.

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: FAIL — `Alt+A` not handled.

- [ ] **Step 3: Extend `handleRowAction`**

In `DesktopShell.tsx`, replace the function:

```tsx
  function handleRowAction(action: RowAction) {
    if (action.type === 'rename') {
      clearPendingDelete()
      if (highlighted) handleRename(highlighted.id)
    } else if (action.type === 'continue') {
      clearPendingDelete()
      if (highlighted) handleQuickResume(highlighted.id)
    } else if (action.type === 'permanent-delete') {
      clearPendingDelete()
      if (highlighted) setSessionDeleteTarget({ id: highlighted.id, name: highlighted.name })
    } else if (action.type === 'archive') {
      if (!highlighted) return
      const id = highlighted.id
      const now = Date.now()
      if (pendingDelete && pendingDelete.id === id && now - pendingDelete.ts < 2000) {
        clearPendingDelete()
        handleClose(id)
      } else {
        if (pendingDeleteTimeoutRef.current) clearTimeout(pendingDeleteTimeoutRef.current)
        setPendingDelete({ id, ts: now })
        pendingDeleteTimeoutRef.current = setTimeout(() => {
          setPendingDelete(null)
          pendingDeleteTimeoutRef.current = null
        }, 2000)
      }
    } else if (action.type === 'jump') {
      clearPendingDelete()
      const s = orderedSessions[action.index]
      if (s) {
        setHighlightedIndex(action.index)
        focusSession(s.id)
      }
    }
  }
```

Add the import:

```tsx
import type { RowAction } from '@/lib/shortcuts'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: PASS.

- [ ] **Step 5: Full typecheck + lint + commit**

Run: `npx tsc -b && npx eslint src/components/DesktopShell.tsx src/components/Dashboard.tsx`
Expected: no errors (the union rename is now consistent everywhere).

```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopShell.test.tsx
git commit -m "feat(web): Alt+C/R/A/D session actions in DesktopShell"
```

---

### Task 5: HelpDrawer slide-out component (reads the registry)

**Files:**
- Create: `web/src/components/HelpDrawer.tsx`
- Test: `web/src/components/__tests__/HelpDrawer.test.tsx`

**Interfaces:**
- Consumes: `SHORTCUTS`, `shortcutsByGroup` from `@/lib/shortcuts`.
- Produces: `function HelpDrawer({ onClose }: { onClose: () => void })` — a right-edge slide-out dialog rendering: (1) shortcut groups, (2) a static Navigation map, (3) static Quick tips.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/HelpDrawer.test.tsx`:

```ts
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HelpDrawer } from '../HelpDrawer'
import { SHORTCUTS } from '@/lib/shortcuts'

describe('HelpDrawer', () => {
  it('renders as a labelled dialog', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
  })

  it('renders a row for every registry shortcut (drift guard)', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    for (const s of SHORTCUTS) {
      expect(screen.getByText(s.description)).toBeInTheDocument()
    }
  })

  it('shows the Alt scheme keys and no longer the old Ctrl bindings', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByText('Alt+A')).toBeInTheDocument()
    expect(screen.getByText('Alt+D')).toBeInTheDocument()
    expect(screen.queryByText('Ctrl+X')).not.toBeInTheDocument()
  })

  it('has Navigation and Tips sections', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('heading', { name: /navigation/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /tips/i })).toBeInTheDocument()
  })

  it('Escape closes', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<HelpDrawer onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('backdrop click closes', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<HelpDrawer onClose={onClose} />)
    await user.click(screen.getByTestId('help-drawer-backdrop'))
    expect(onClose).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/HelpDrawer.test.tsx`
Expected: FAIL — `Cannot find module '../HelpDrawer'`.

- [ ] **Step 3: Implement HelpDrawer**

Create `web/src/components/HelpDrawer.tsx`. Reuse the Esc + Tab-trap pattern from the old `ShortcutsHelp.tsx` (lines 36-73) but render a right slide-out and source rows from the registry:

```tsx
import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { shortcutsByGroup, type Shortcut } from '@/lib/shortcuts'

const NAV_MAP: { name: string; detail: string }[] = [
  { name: 'Sessions', detail: 'Live sessions list — the default view' },
  { name: 'Accounts', detail: 'Manage Claude accounts and rotation' },
  { name: 'Archived', detail: 'Review, unarchive, or permanently delete past sessions' },
  { name: 'Session view', detail: 'Open a session to read its transcript and send prompts' },
  { name: 'Review', detail: 'Read-only transcript of an archived session' },
]

const TIPS: { title: string; detail: string }[] = [
  { title: 'Accounts = config dirs', detail: 'Each account is an isolated CLAUDE_CONFIG_DIR; ccswitch rotates them as usage allows.' },
  { title: 'Archive vs delete', detail: 'Archive (Alt+A) hides a session but keeps it; delete (Alt+D) removes it permanently.' },
  { title: 'Quick-continue', detail: 'Alt+C resumes the highlighted session by sending “continue” without opening it.' },
  { title: 'Permission modes', detail: 'Inside a session, Shift+Tab cycles the permission mode.' },
]

export function HelpDrawer({ onClose }: { onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => { closeButtonRef.current?.focus() }, [])

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      data-testid="help-drawer-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-drawer-title"
        className={cn(
          'h-full w-full max-w-md overflow-y-auto p-6 space-y-6',
          'bg-white dark:bg-gray-800 border-l border-gray-200 dark:border-gray-600 shadow-xl',
        )}
      >
        <div className="flex items-center justify-between">
          <h2 id="help-drawer-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Help &amp; Shortcuts
          </h2>
          <button
            ref={closeButtonRef}
            type="button"
            aria-label="Close help"
            onClick={onClose}
            className={cn(
              'h-8 w-8 flex items-center justify-center rounded-md text-gray-500 dark:text-gray-400',
              'hover:bg-gray-100 dark:hover:bg-gray-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            )}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <ShortcutSection title="Sessions list" bindings={shortcutsByGroup('sessions-list')} />
        <ShortcutSection title="In-session" bindings={shortcutsByGroup('in-session')} />

        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">Navigation</h3>
          <dl className="space-y-1">
            {NAV_MAP.map((n) => (
              <div key={n.name} className="flex items-baseline gap-3">
                <dt className="shrink-0 min-w-[110px] text-sm font-medium text-gray-700 dark:text-gray-300">{n.name}</dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{n.detail}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">Tips</h3>
          <dl className="space-y-2">
            {TIPS.map((t) => (
              <div key={t.title}>
                <dt className="text-sm font-medium text-gray-700 dark:text-gray-300">{t.title}</dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{t.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </div>
  )
}

function ShortcutSection({ title, bindings }: { title: string; bindings: Shortcut[] }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{title}</h3>
      <dl className="space-y-1">
        {bindings.map((b) => (
          <div key={b.id} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-[100px]">
              <kbd className={cn(
                'inline-block rounded px-1.5 py-0.5 text-xs font-mono',
                'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300',
                'border border-gray-300 dark:border-gray-600',
              )}>{b.keys}</kbd>
            </dt>
            <dd className="text-sm text-gray-700 dark:text-gray-300">{b.description}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/__tests__/HelpDrawer.test.tsx`
Expected: PASS (6 tests).

- [ ] **Step 5: Typecheck + lint + commit**

Run: `npx tsc -b && npx eslint src/components/HelpDrawer.tsx`
Expected: no errors.

```bash
git add web/src/components/HelpDrawer.tsx web/src/components/__tests__/HelpDrawer.test.tsx
git commit -m "feat(web): HelpDrawer slide-out rendered from the shortcuts registry"
```

---

### Task 6: Integrate HelpDrawer, delete ShortcutsHelp, add visible button + global '?'

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (`ShortcutsHelp` import + render ~1984; add help button)
- Modify: `web/src/components/DesktopShell.tsx` (`ShortcutsHelp` import ~27 + render ~513; add help button)
- Create: `web/src/components/useGlobalHelpKey.ts`
- Delete: `web/src/components/ShortcutsHelp.tsx` and `web/src/components/__tests__/ShortcutsHelp.test.tsx`
- Test: `web/src/components/__tests__/useGlobalHelpKey.test.tsx`

**Interfaces:**
- Consumes: `HelpDrawer` (Task 5); existing `showHelp`/`setShowHelp` state in both shells.
- Produces: `function useGlobalHelpKey({ enabled, onShowHelp }: { enabled: boolean; onShowHelp: () => void }): void`.

- [ ] **Step 1: Write the failing hook test**

Create `web/src/components/__tests__/useGlobalHelpKey.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useGlobalHelpKey } from '../useGlobalHelpKey'

function Harness({ onShowHelp }: { onShowHelp: () => void }) {
  useGlobalHelpKey({ enabled: true, onShowHelp })
  return <input aria-label="field" />
}

describe('useGlobalHelpKey', () => {
  it('opens help on ? when focus is not in a field', async () => {
    const onShowHelp = vi.fn()
    const user = userEvent.setup()
    render(<Harness onShowHelp={onShowHelp} />)
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalledTimes(1)
  })

  it('does NOT open help when typing ? into an input', async () => {
    const onShowHelp = vi.fn()
    const user = userEvent.setup()
    const { getByLabelText } = render(<Harness onShowHelp={onShowHelp} />)
    await user.click(getByLabelText('field'))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/__tests__/useGlobalHelpKey.test.tsx`
Expected: FAIL — `Cannot find module '../useGlobalHelpKey'`.

- [ ] **Step 3: Implement the hook**

Create `web/src/components/useGlobalHelpKey.ts` (mirrors `useGlobalListNav`'s guards):

```ts
import { useEffect } from 'react'

interface Options {
  enabled: boolean
  onShowHelp: () => void
}

/**
 * `?` opens the help drawer from anywhere — except while typing in a field or when a
 * dialog is already open (so a literal `?` in a prompt still types, and the drawer's own
 * Esc/Tab handling isn't shadowed).
 */
export function useGlobalHelpKey({ enabled, onShowHelp }: Options): void {
  useEffect(() => {
    if (!enabled) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== '?' && !(e.key === '/' && e.shiftKey)) return
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      onShowHelp()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onShowHelp])
}
```

- [ ] **Step 4: Run hook test to verify it passes**

Run: `npx vitest run src/components/__tests__/useGlobalHelpKey.test.tsx`
Expected: PASS.

- [ ] **Step 5: Swap ShortcutsHelp → HelpDrawer + add button + wire global '?' (Dashboard)**

In `Dashboard.tsx`:
- Replace the import of `ShortcutsHelp` with `import { HelpDrawer } from './HelpDrawer'` and add `import { useGlobalHelpKey } from './useGlobalHelpKey'`.
- Replace the render (~1984):

```tsx
      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}
```

- Near the existing `useGlobalListNav(...)` call (~1479) add:

```tsx
  useGlobalHelpKey({ enabled: activeTab === 'sessions', onShowHelp: () => setShowHelp(true) })
```

- Add a visible help button in the dashboard header/chrome (place beside the existing tab/header controls). Use the existing `Button` primitive:

```tsx
        <button
          type="button"
          aria-label="Open help"
          onClick={() => setShowHelp(true)}
          className={cn(
            'h-9 w-9 flex items-center justify-center rounded-md',
            'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
        >
          <HelpCircle size={18} aria-hidden="true" />
        </button>
```

Add `HelpCircle` to the existing `lucide-react` import in `Dashboard.tsx`.

- [ ] **Step 6: Swap + add button (DesktopShell)**

In `DesktopShell.tsx`:
- Replace `import { ShortcutsHelp } from './ShortcutsHelp'` (line 27) with `import { HelpDrawer } from './HelpDrawer'` and add `import { useGlobalHelpKey } from './useGlobalHelpKey'`.
- Replace the render (~513): `{showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}`.
- Add `useGlobalHelpKey({ enabled: tab === 'sessions', onShowHelp: () => setShowHelp(true) })` near the other hooks.
- Add the same visible help button (with `HelpCircle`) to the desktop sidebar/header chrome.

- [ ] **Step 7: Delete the old modal + its test**

```bash
git rm web/src/components/ShortcutsHelp.tsx web/src/components/__tests__/ShortcutsHelp.test.tsx
```

Then grep to confirm no remaining references:

Run: `grep -rn "ShortcutsHelp" web/src`
Expected: no output.

- [ ] **Step 8: Full verification**

Run from `web/`:
```bash
npx tsc -b
npx vitest run
npx eslint src/components/Dashboard.tsx src/components/DesktopShell.tsx src/components/HelpDrawer.tsx src/components/useGlobalHelpKey.ts
```
Expected: typecheck clean; full suite green; eslint clean (`--max-warnings 0`).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(web): global help slide-out replaces shortcuts modal; visible help button + global ?"
```

---

## Self-Review

**Spec coverage:**
- Alt scheme (continue/rename/archive/delete) → Tasks 1-4. ✓
- `Ctrl+R`/`Ctrl+X` removed → Task 2 (handler) + Task 5/6 (help no longer lists them). ✓
- Press-twice archive guard preserved → Tasks 3 & 4 (`archive` branch). ✓
- Permanent delete routes through `PermanentDeleteDialog` (never instant) → Tasks 3 & 4 (`permanent-delete` → `handleSessionDelete`/`setSessionDeleteTarget`). ✓
- Quick-continue sends `continue` via `useSendPrompt` → Tasks 3 & 4 (`continue` → `handleQuickResume`). ✓
- Single registry feeding handler + help (no drift) → Task 1 + Task 2 (matching) + Task 5 (drift-guard test). ✓
- Help slide-out replaces modal, 3 content pillars (shortcuts/navigation/tips) → Task 5; old modal deleted → Task 6 Step 7. ✓
- Triggers: global `?` + visible button → Task 6 (`useGlobalHelpKey` + button). ✓
- Both Dashboard & DesktopShell updated → Tasks 3, 4, 6. ✓
- `preventDefault` on Alt matches; `code`-based matching for Mac → Task 1 (`altCode`) + Task 2 (handler). ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. Test-helper names (`renderDashboard`, `NOOP_MUTATION`, `renderDesktopShell`) are flagged to be matched to each file's existing harness rather than invented — acceptable because those files already define them.

**Type consistency:** `RowAction`/`SessionRowAction` defined in Task 1 and imported in Tasks 2-4; `'delete'` → `'archive'` rename applied consistently in the prop type and both `handleRowAction`s; `HelpDrawer`/`useGlobalHelpKey` signatures match their usage in Task 6.

**Commit-green note:** The `'delete'`→`'archive'` rename spans Tasks 2-4; if your review gate requires each commit to typecheck, land Tasks 2-4 as a single commit (see Task 2 Step 7 note).
