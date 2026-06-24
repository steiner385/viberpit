# In-Session Archive & Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user archive or permanently delete the session they are currently viewing, from inside the session view (mobile full-screen and desktop master-detail).

**Architecture:** A new shared `SessionActions` component renders an Archive button plus an overflow "⋯" menu (Delete) in both session headers. It routes through the *existing* `useCloseSession` / `usePermanentDeleteSession` hooks and the *existing* single-session confirm dialog. Delete is archive-then-delete (close → on success → permanent-delete) so the live driver is torn down cleanly. After a successful archive or delete, the host detaches the socket and navigates back to the list. No backend changes.

**Tech Stack:** React + TypeScript, TanStack Query, Tailwind, lucide-react icons, vitest + Testing Library.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-in-session-archive-delete-design.md`.
- No new backend endpoints/orchestrator methods — reuse `DELETE /api/sessions/{id}` (archive) and `DELETE /api/sessions/{id}/permanent` (delete).
- Single source of truth: there must be exactly **one** single-session `PermanentDeleteDialog` after Task 1. Do not add a parallel copy. (ArchivedManager's *separate* bulk-capable copy is out of scope and stays.)
- Archive acts immediately (no confirm; reversible). Delete always confirms via `PermanentDeleteDialog`.
- On any mutation error, do **not** call `onDone` (the user stays in the session).
- Web gates (run in `web/`): `npx tsc -b --force` (the real type gate — `tsc --noEmit` checks nothing here) and `eslint` with `--max-warnings 0`.
- Test runner in the worktree: use the symlinked local binary `./node_modules/.bin/vitest` / `./node_modules/.bin/eslint`, not `npx`.
- Touch-target minimum: 44px (`min-h-[44px]`), matching existing header buttons.

---

### Task 1: Extract the single-session `PermanentDeleteDialog` into a shared module

Pure refactor, no behavior change. Today `PermanentDeleteDialog` is defined in `Dashboard.tsx` and imported by `DesktopShell.tsx`. Move it to its own module so `SessionActions` can import it without pulling in all of `Dashboard`.

**Files:**
- Create: `web/src/components/PermanentDeleteDialog.tsx`
- Modify: `web/src/components/Dashboard.tsx` (remove the local definition at lines 869–964; import from the new module instead)
- Modify: `web/src/components/DesktopShell.tsx:34` (import `PermanentDeleteDialog` from the new module; keep `RenameDialog` from `./Dashboard`)

**Interfaces:**
- Produces: `PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: { sessionName: string; onClose: () => void; onConfirm: () => void; isPending: boolean })` from `@/components/PermanentDeleteDialog`.

- [ ] **Step 1: Create the shared module**

Create `web/src/components/PermanentDeleteDialog.tsx` with the exact markup moved from `Dashboard.tsx:869–964` (converting `React.useEffect` → `useEffect`):

```tsx
/**
 * PermanentDeleteDialog — single-session destructive confirm.
 *
 * Shared source of truth for the "permanently delete this session" modal, used by
 * Dashboard (session card + live session), DesktopShell, and the in-session
 * SessionActions menu. Focus starts on Cancel; Esc closes; Tab is focus-trapped.
 *
 * NOTE: ArchivedManager.tsx still carries its own bulk-capable copy; consolidating
 * that one is tracked separately.
 */
import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

export function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perm-delete-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-sm rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
        )}
      >
        <h2
          id="perm-delete-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Permanently delete session
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          Permanently delete <span className="font-medium">'{sessionName}'</span>? This removes the
          session folder and all its data. This can't be undone.
        </p>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className={cn(
              'min-h-[44px]',
              'bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600',
              'focus-visible:ring-red-500',
            )}
          >
            {isPending ? 'Deleting…' : 'Delete permanently'}
          </Button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Remove the old definition from `Dashboard.tsx`**

Delete `Dashboard.tsx` lines 869–964 (the `PermanentDeleteDialogProps` interface, the `// Exported so DesktopShell…` comment, and the `export function PermanentDeleteDialog` body). Then add to Dashboard's import block (near the other `@/components` imports):

```tsx
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
```

Leave Dashboard's two existing usages (`<PermanentDeleteDialog … />` around lines 1958 and 1967) unchanged — they now resolve to the imported component.

- [ ] **Step 3: Update the `DesktopShell.tsx` import**

Change line 34 from:

```tsx
import { RenameDialog, PermanentDeleteDialog } from './Dashboard'
```

to:

```tsx
import { RenameDialog } from './Dashboard'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
```

- [ ] **Step 4: Type-check + lint**

Run (in `web/`):
```bash
npx tsc -b --force
./node_modules/.bin/eslint src/components/PermanentDeleteDialog.tsx src/components/Dashboard.tsx src/components/DesktopShell.tsx --max-warnings 0
```
Expected: both clean (no errors).

- [ ] **Step 5: Run the dialog's existing consumers' tests to prove no behavior change**

```bash
./node_modules/.bin/vitest run src/components/__tests__/AccountDelete.test.tsx src/App.dialogs.test.tsx
```
Expected: PASS (these render Dashboard/DesktopShell delete dialogs).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/PermanentDeleteDialog.tsx web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx
git commit -m "refactor(web): extract single-session PermanentDeleteDialog into shared module"
```

---

### Task 2: Build the `SessionActions` component (TDD)

The core unit: Archive button + overflow "⋯" menu (Delete) + archive-then-delete logic + confirm dialog.

**Files:**
- Create: `web/src/components/session/SessionActions.tsx`
- Test: `web/src/components/session/SessionActions.test.tsx`

**Interfaces:**
- Consumes: `useCloseSession`, `usePermanentDeleteSession` from `@/api/hooks`; `PermanentDeleteDialog` from `@/components/PermanentDeleteDialog` (Task 1).
- Produces: `SessionActions({ sessionId, sessionName, onDone, variant }: { sessionId: string; sessionName: string; onDone: () => void; variant?: 'dark' | 'light' })` from `@/components/session/SessionActions`. `onDone` is called only after a *successful* archive or a *successful* delete.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/SessionActions.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionActions } from './SessionActions'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
}))

// mutate(id, opts) helpers that synchronously invoke the success/error callbacks,
// so we can assert the archive-then-delete sequencing without async plumbing.
function successMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}
function errorMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onError?: (e: unknown) => void }) => opts?.onError?.(new Error('boom'))), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SessionActions', () => {
  it('Archive: clicking archives the session then calls onDone (no dialog)', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /archive session my session/i }))

    expect(close.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(perm.mutate).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('Delete: opens confirm, then archive-then-delete in order, then onDone', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))

    // Confirm dialog appears; onDone not called yet.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(close.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(perm.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(close.mutate.mock.invocationCallOrder[0]).toBeLessThan(perm.mutate.mock.invocationCallOrder[0])
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('Delete: if archive fails, it does NOT permanent-delete and does NOT call onDone', async () => {
    const user = userEvent.setup()
    const close = errorMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(perm.mutate).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('Delete: Cancel closes the dialog without mutating', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(close.mutate).not.toHaveBeenCalled()
    expect(perm.mutate).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./node_modules/.bin/vitest run src/components/session/SessionActions.test.tsx
```
Expected: FAIL — cannot resolve `./SessionActions`.

- [ ] **Step 3: Write the component**

Create `web/src/components/session/SessionActions.tsx`:

```tsx
/**
 * SessionActions — in-session Archive + Delete controls for the session header.
 *
 * Archive is a primary header button (reversible → no confirm). Delete lives behind an
 * overflow "⋯" menu and is destructive → it confirms via the shared PermanentDeleteDialog.
 * Delete is archive-then-delete: archive first (stops the live driver, moves the dir to the
 * archive), and only on success permanently delete — so we never leave a zombie driver. On a
 * successful archive or delete we call onDone() so the host detaches the socket and navigates
 * back to the list. Used by both session headers: session/SessionView.tsx (mobile, dark bar)
 * and DesktopShell's MainPaneSession (desktop, light bar). The dropdown mirrors ExportMenu's
 * lightweight role="menu" pattern.
 */
import { useEffect, useRef, useState } from 'react'
import { Archive, MoreVertical, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCloseSession, usePermanentDeleteSession } from '@/api/hooks'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'

interface Props {
  sessionId: string
  sessionName: string
  /** Called after a successful archive or permanent delete (host detaches + navigates). */
  onDone: () => void
  /** Header colour scheme: 'dark' for the mobile bar (gray-900), 'light' for desktop. */
  variant?: 'dark' | 'light'
}

export function SessionActions({ sessionId, sessionName, onDone, variant = 'dark' }: Props) {
  const closeSession = useCloseSession()
  const permanentDelete = usePermanentDeleteSession()
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close the overflow menu on outside-click / Escape (mirrors ExportMenu).
  useEffect(() => {
    if (!menuOpen) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const isDark = variant === 'dark'
  const iconBtn = cn(
    'inline-flex items-center justify-center rounded-md min-h-[44px] min-w-[44px]',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
    isDark
      ? 'text-gray-200 hover:bg-gray-700 active:bg-gray-600'
      : 'text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700',
  )

  function handleArchive() {
    closeSession.mutate(sessionId, { onSuccess: onDone })
  }

  function handleDeleteConfirm() {
    // Archive-then-delete: stop + archive the live session, then permanently delete.
    closeSession.mutate(sessionId, {
      onSuccess: () => {
        permanentDelete.mutate(sessionId, {
          onSuccess: () => { setConfirmOpen(false); onDone() },
        })
      },
    })
  }

  const deletePending = closeSession.isPending || permanentDelete.isPending

  return (
    <div className="flex items-center gap-1" ref={menuRef}>
      <button
        type="button"
        onClick={handleArchive}
        disabled={closeSession.isPending}
        aria-label={`Archive session ${sessionName}`}
        className={iconBtn}
      >
        <Archive size={18} aria-hidden="true" />
      </button>

      <div className="relative">
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="More session actions"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className={iconBtn}
        >
          <MoreVertical size={18} aria-hidden="true" />
        </button>

        {menuOpen && (
          <div
            role="menu"
            aria-label="Session actions"
            className={cn(
              'absolute right-0 top-full z-10 mt-1 w-44',
              'rounded-md border border-gray-200 dark:border-gray-700',
              'bg-white dark:bg-gray-900 shadow-lg py-1',
            )}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => { setMenuOpen(false); setConfirmOpen(true) }}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                'text-red-600 dark:text-red-400',
                'hover:bg-gray-100 dark:hover:bg-gray-800',
                'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
              )}
            >
              <Trash2 size={16} aria-hidden="true" />
              Delete permanently
            </button>
          </div>
        )}
      </div>

      {confirmOpen && (
        <PermanentDeleteDialog
          sessionName={sessionName}
          onClose={() => setConfirmOpen(false)}
          onConfirm={handleDeleteConfirm}
          isPending={deletePending}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./node_modules/.bin/vitest run src/components/session/SessionActions.test.tsx
```
Expected: PASS (4 tests).

Note: the confirm dialog's destructive button label is "Delete permanently", which also matches the menu item text — the test scopes the menu click with `role: 'menuitem'` and the confirm click with `role: 'button'`, so they don't collide.

- [ ] **Step 5: Type-check + lint**

```bash
npx tsc -b --force
./node_modules/.bin/eslint src/components/session/SessionActions.tsx src/components/session/SessionActions.test.tsx --max-warnings 0
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/SessionActions.tsx web/src/components/session/SessionActions.test.tsx
git commit -m "feat(web): add SessionActions (in-session archive + delete) component"
```

---

### Task 3: Wire `SessionActions` into the mobile `SessionView` header

**Files:**
- Modify: `web/src/components/session/SessionView.tsx` (header at lines 115–136; `handleBack` at 51–54)
- Test: `web/src/components/session/__tests__/SessionViewActions.test.tsx` (new)

**Interfaces:**
- Consumes: `SessionActions` (Task 2). The mobile bar is dark (`bg-gray-900`), so use `variant="dark"`. `onDone={handleBack}` — `handleBack` already detaches the socket and calls `onBack()` (which navigates to `/sessions`).

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/SessionViewActions.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionView } from '../SessionView'
import * as hooks from '@/api/hooks'

const detach = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({ detach }),
}))

// Minimal runtime stub — SessionView only needs these fields for the header + body shells.
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    title: 'My Session',
    mode: 'interactive',
    hasOpenAction: false,
    onSetMode: vi.fn(),
    commandResult: null,
    commands: [],
    dismissCommandResult: vi.fn(),
    state: { transcript: [], turns: [], status: 'idle', model: null },
  }),
}))

// SessionBody / SessionComposer are heavy; stub them so we test only the header wiring.
vi.mock('../SessionBody', () => ({ SessionBody: () => <div data-testid="body" /> }))
vi.mock('../SessionComposer', () => ({ SessionComposer: () => <div data-testid="composer" /> }))
vi.mock('../CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
}))

function successMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(hooks.useCloseSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
})

describe('SessionView header — archive/delete', () => {
  it('renders the in-session Archive control', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: /archive session my session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more session actions/i })).toBeInTheDocument()
  })

  it('archiving detaches the socket and navigates back', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />)

    await user.click(screen.getByRole('button', { name: /archive session my session/i }))

    expect(detach).toHaveBeenCalledWith('s1')
    expect(onBack).toHaveBeenCalledTimes(1)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./node_modules/.bin/vitest run src/components/session/__tests__/SessionViewActions.test.tsx
```
Expected: FAIL — no "Archive session" button yet.

- [ ] **Step 3: Add the import**

In `SessionView.tsx`, add to the import block (after the other `./` component imports):

```tsx
import { SessionActions } from '@/components/session/SessionActions'
```

- [ ] **Step 4: Render `SessionActions` in the header**

In `SessionView.tsx`, replace the title line (currently the last child of the top-bar `div`, line 135):

```tsx
        <span className="text-sm font-semibold text-gray-100 truncate">{rt.title}</span>
```

with the title plus a right-aligned actions cluster:

```tsx
        <span className="flex-1 text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

        <SessionActions
          sessionId={sessionId}
          sessionName={rt.title}
          onDone={handleBack}
          variant="dark"
        />
```

(The top-bar container already has `flex items-center gap-3`; adding `flex-1` to the title pushes the actions to the right edge.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
./node_modules/.bin/vitest run src/components/session/__tests__/SessionViewActions.test.tsx
```
Expected: PASS (2 tests).

- [ ] **Step 6: Type-check + lint**

```bash
npx tsc -b --force
./node_modules/.bin/eslint src/components/session/SessionView.tsx src/components/session/__tests__/SessionViewActions.test.tsx --max-warnings 0
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/session/__tests__/SessionViewActions.test.tsx
git commit -m "feat(web): surface archive/delete in the mobile SessionView header"
```

---

### Task 4: Wire `SessionActions` into the desktop `MainPaneSession` header

**Files:**
- Modify: `web/src/components/DesktopShell.tsx` (`MainPaneSession` at lines 59–79; render site at line 434; add a close handler)

**Interfaces:**
- Consumes: `SessionActions` (Task 2). Desktop bar is light (`bg-gray-100`), so `variant="light"`. `onDone` clears the selection back to the new-session slot; the keyed `SessionRuntimeProvider` unmounts on selection change, which detaches the socket.

- [ ] **Step 1: Write the failing test**

Add to `web/src/components/__tests__/` a focused test. Create `web/src/components/__tests__/DesktopSessionActions.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MainPaneSession } from '../DesktopShell'

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

// Provide a runtime via the context the pane reads from. DesktopShell imports both
// the provider and the hook from ./session/SessionRuntimeProvider, so mock that module
// (keep SessionRuntimeProvider as a pass-through so other importers don't break).
vi.mock('../session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useSessionRuntimeContext: () => ({
    title: 'Desk Session',
    commandResult: null,
    commands: [],
    dismissCommandResult: vi.fn(),
  }),
}))
vi.mock('../session/SessionBody', () => ({ SessionBody: () => <div /> }))
vi.mock('../session/CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

beforeEach(() => vi.clearAllMocks())

describe('Desktop MainPaneSession header — archive/delete', () => {
  it('renders the in-session Archive + more-actions controls', () => {
    render(<MainPaneSession sessionId="s1" onClosed={vi.fn()} />)
    expect(screen.getByRole('button', { name: /archive session desk session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more session actions/i })).toBeInTheDocument()
  })
})
```

> `MainPaneSession` is currently an unexported module-level function; export it as part of Step 3 (add `export` to `function MainPaneSession`). The runtime context hook `useSessionRuntimeContext` is imported by DesktopShell from `./session/SessionRuntimeProvider` (verified), which is the module the test mocks.

- [ ] **Step 2: Run the test to verify it fails**

```bash
./node_modules/.bin/vitest run src/components/__tests__/DesktopSessionActions.test.tsx
```
Expected: FAIL — `MainPaneSession` not exported / no Archive button.

- [ ] **Step 3: Update `MainPaneSession` to take an `onClosed` prop and render actions**

In `DesktopShell.tsx`, add the import (with the other `@/components` imports):

```tsx
import { SessionActions } from '@/components/session/SessionActions'
```

Change `MainPaneSession` (lines 59–79) to accept `sessionId` + `onClosed` and render `SessionActions` in its header. Export it for the test:

```tsx
export function MainPaneSession({ sessionId, onClosed }: { sessionId: string; onClosed: () => void }) {
  const rt = useSessionRuntimeContext()
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="flex-1 text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
        <SessionActions
          sessionId={sessionId}
          sessionName={rt.title}
          onDone={onClosed}
          variant="light"
        />
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-white dark:bg-gray-950">
        <SessionBody rt={rt} />
      </div>
      <CommandResultOverlay
        result={rt.commandResult}
        commands={rt.commands}
        onClose={rt.dismissCommandResult}
      />
    </div>
  )
}
```

- [ ] **Step 4: Pass the props at the render site**

In `DesktopShell` (the `mainPane` assignment, line 434), change:

```tsx
    mainPane = selection.type === 'session' ? <MainPaneSession /> : <WelcomePane />
```

to:

```tsx
    mainPane = selection.type === 'session'
      ? <MainPaneSession sessionId={selection.id} onClosed={() => setSelection({ type: 'new' })} />
      : <WelcomePane />
```

(`setSelection({ type: 'new' })` mirrors the existing `handleClose`/`focusNew` behavior and unmounts the keyed `SessionRuntimeProvider`, detaching the socket. This is the same teardown the existing list-archive uses, so no new detach path is introduced.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
./node_modules/.bin/vitest run src/components/__tests__/DesktopSessionActions.test.tsx
```
Expected: PASS.

- [ ] **Step 6: Full web gate (type, lint, full vitest)**

CI runs the full vitest suite, so run it all to catch fixture interactions (see the project's "full vitest" note):

```bash
npx tsc -b --force
./node_modules/.bin/eslint src --max-warnings 0
./node_modules/.bin/vitest run
```
Expected: all green. (CI-only `react-router-dom`/`@novnc` tests for SessionView/App/SubSession/SessionCard may be skipped/inert in the offline worktree — that is expected per project notes; everything else must pass.)

- [ ] **Step 7: Commit**

```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopSessionActions.test.tsx
git commit -m "feat(web): surface archive/delete in the desktop session header"
```

---

## Self-Review

**1. Spec coverage**
- "Archive primary in header, Delete in overflow menu" → Task 2 (`SessionActions` layout) + Tasks 3–4 (both headers). ✓
- "Delete = archive-then-delete" → Task 2 `handleDeleteConfirm` (close → on success → permanent-delete), test asserts call order. ✓
- "Archive immediate, no confirm; Delete confirms" → Task 2 (`handleArchive` direct; `confirmOpen` dialog for delete). ✓
- "Reuse existing hooks + existing PermanentDeleteDialog; no parallel impl" → Task 1 extracts the single shared dialog; Task 2 imports it + the existing hooks. ✓
- "Render in both mobile + desktop headers" → Tasks 3 (mobile `SessionView`) + 4 (desktop `MainPaneSession`). ✓
- "On success detach + navigate back; on error stay" → mobile `onDone=handleBack` (detach+navigate); desktop `onDone` clears selection (unmount → detach); error paths never call `onDone` (Task 2 test). ✓
- "Cache invalidation handled in hooks" → unchanged `useCloseSession` / `usePermanentDeleteSession`. ✓
- Testing (vitest on SessionActions + header wiring; tsc -b + eslint) → each task's gate steps. ✓
- Backend unchanged → confirmed no server files in any task. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code. The only conditional instruction (Task 4 Step 1 note about export paths) is an explicit verify-with-grep instruction, not a placeholder.

**3. Type consistency:** `SessionActions` props `{ sessionId, sessionName, onDone, variant }` are used identically in Tasks 3 and 4. `PermanentDeleteDialog` props `{ sessionName, onClose, onConfirm, isPending }` match the moved definition (Task 1) and the consumer (Task 2). `MainPaneSession` gains `{ sessionId, onClosed }` consistently in Steps 3 and 4 of Task 4.
