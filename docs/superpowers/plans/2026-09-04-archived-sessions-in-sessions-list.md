# Archived Sessions in the Sessions List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Archived a *scope* of the Sessions list — an Active/Archived switch inside the Sessions column — instead of a separate top-level page with its own duplicated card, bulk-bar, selection and dialog code.

**Architecture:** Three sequential PRs. PR 1 extracts the list chrome both lists already duplicate (selection, bulk bar, checkbox row, empty states, delete dialog) and rewrites both call sites onto it. PR 2 extracts the card *frame* out of `SessionCard` (847 lines), builds `ArchivedSessionCard` on it, and replaces `ArchivedManager` with `ArchivedSessionList`. PR 3 moves the archived routes under `/sessions/archived`, adds the scope switch to `SessionListColumn`, and deletes the `archived` nav entry. Scope is derived from the route — no query param, no local state. The two data streams stay separate: the live 3–5 s poll never carries archived rows.

**Tech Stack:** React 19.2 · TypeScript 6.0 · Vite 8 · Tailwind v4 (`@theme` tokens) · react-router-dom 7.18 · @tanstack/react-query 5 · vitest + @testing-library/react + jest-axe · FastAPI + SQLAlchemy 2.0 (one backend field fix).

**Spec:** `docs/superpowers/specs/2026-09-04-archived-sessions-in-sessions-list-design.md` — **read it first.** Decisions D1–D8 there are binding; this plan implements them and does not restate their reasoning.

---

## Global Constraints

Copied from the spec, CLAUDE.md and the repo's existing gates. Every task's requirements implicitly include this section.

- **Replaced code is deleted in the same PR.** No `@deprecated` shims, no half-dead modules. When a task says "delete X", X is gone at the end of that task. (CLAUDE.md "Purge replaced code in the same change")
- **Before adding a constant, helper, config list or component, grep for the existing source of truth and route through it.** Adding a parallel "correct" copy *is* the anti-pattern this whole plan exists to remove. (CLAUDE.md)
- **ESLint `ccswitch/no-raw-palette` governs `src/ui/**`, `src/shell/**` and `src/features/**` — NOT `src/components/**`.** This decides where new files go:
  - Shared *chrome* extracted from `SessionCard`/`SessionList` goes under `web/src/components/` (`components/list/`, `components/card/`), where the raw palette classes it carries verbatim are still legal. Extractions must be **byte-identical class strings** — this plan never restyles while extracting.
  - New files under `web/src/features/sessions/` (`ScopeSegment.tsx`, `useArchivedListState.ts`) MUST use design tokens: `border-edge`, `text-ink`, `text-ink-muted`, `bg-surface`, `bg-surface-raised`, `bg-accent-wash`, `text-accent`, `border-accent`, `min-h-target`, `focus-visible:ring-accent`. No `dark:`, no hex, no z-index literals, no arbitrary px.
- **`ArchivedSession` stays a distinct type.** Do not widen `SessionStatus` to include `'archived'`, and do not add `archived` to `INACTIVE_SESSION_STATUSES` handling on the client. The exclusion is what keeps the mirror-zombie guard (#1033, #898/#900) intact. (D5)
- **Archived and live queries stay separate.** `useSessions()` (3 s stale / 5 s poll, WS-reconciled) and `useArchivedSessions()` (10 s stale / 30 s poll) are never merged. (D4)
- **Run the FULL web suite, never changed-files-only.** Adding a hook to a shared component breaks distant suites that hand-mock `@/api/hooks` — this bit PR #938 and it will bite Task 13. (memory: new-hook-in-shared-component-breaks-manual-mocks)
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Implementers never push and never open PRs.** The controller does that.

### Gates (all must pass before any task reports DONE)

```
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
```

Use **exactly these npm scripts**. A bare `tsc --noEmit` skips test files and passes while CI's `tsc -b` fails (PR #1087). Tasks 10 and 14 additionally touch Python; those run the four-step gate: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.

**Environment notes (this worktree):**
- Run web commands as `npm --prefix web <cmd>` from the worktree root. **Never `cd web/`** — a repo PreToolUse hook is cwd-relative and locks out all Bash. Use `(cd web && …)` subshells if you must.
- Python runs against the main checkout's prebuilt env: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …`, with all `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` unset.
- `web/node_modules` is symlinked from the main checkout.
- **Push after every task commit.** The daemon can re-clone and relink worktrees, dropping unpushed commits (7 were lost on PR #1085).

---

## File Structure

### PR 1 — shared list chrome

| File | Responsibility |
|---|---|
| `web/src/components/list/useBulkSelection.ts` **(new)** | Selection set, anchor, shift-range over the visible order, stale-id prune. No JSX. |
| `web/src/components/list/BulkActionBar.tsx` **(new)** | The `region[aria-label="Bulk actions"]` shell: count label, `children` slot for actions, Clear. |
| `web/src/components/list/SelectableRow.tsx` **(new)** | Checkbox + shift-capture wrapper around any row. Exports `SELECT_CHECKBOX_CLS`. |
| `web/src/components/list/SkeletonCard.tsx` **(new)** | Moved verbatim out of `SessionList`. |
| `web/src/components/list/NoMatchState.tsx` **(new)** | The dashed "nothing matched" panel + clear action. |
| `web/src/components/PermanentDeleteDialog.tsx` *(modify)* | Absorbs the archived inline copy: `names: string[]`, optional `progress` + `onCancel`. |
| `web/src/components/SessionList.tsx` *(modify)* | Loses its BulkActionBar, selection block, `CHECKBOX_CLS`, `SkeletonCard`, dashed panel. |
| `web/src/components/ArchivedManager.tsx` *(modify)* | Loses its BulkActionBar, selection block, `CHECKBOX_CLS`, inline dialog, empty states. |
| `web/src/shell/AppShell.tsx`, `web/src/components/session/SessionActions.tsx` *(modify)* | `sessionName={x}` → `names={[x]}`. |

### PR 2 — shared card chrome + the archived card

| File | Responsibility |
|---|---|
| `web/src/components/card/CardFrame.tsx` **(new)** | The `<article>`: status rail, swipe panels + foreground, selected ring, scroll-into-view, list-mode focus, click-to-open fallthrough, the open button. |
| `web/src/components/card/CardMenu.tsx` **(new)** | The ⋯ trigger + popup: outside-click, Escape (stopPropagation), overlay push/pop, focus-the-trigger. Exports `menuItem`, `actionBtn`, `Sep`. |
| `web/src/components/SessionCard.tsx` *(modify)* | Rewritten onto `CardFrame` + `CardMenu`. **No behaviour change** — its 12 existing test files are the net. |
| `web/src/components/ArchivedSessionCard.tsx` **(new)** | Muted rail, name · account, repo · "Archived 3d ago", swipe unarchive/delete, ⋯ menu. |
| `web/src/components/ArchivedSessionList.tsx` **(new)** | Replaces `ArchivedManager`: mutations, select-all header, quick purge, bulk bar, dialogs. |
| `web/src/components/ArchivedManager.tsx` **(deleted)** | |
| `server/sessions/orchestrator.py` *(modify)* | `list_archived` serves `archived_at`, falling back to `updated_at`. |

### PR 3 — the scope switch

| File | Responsibility |
|---|---|
| `web/src/session/sessionNavState.ts` *(modify)* | `ARCHIVED_PATH` → `/sessions/archived`; `ARCHIVED_SESSION_ROUTE` uses `:archivedId`; adds `listScopeForPath`. |
| `web/src/shell/openSessionId.ts` *(modify)* | Adds `useOpenItem()`; `useOpenSessionId()` derives from it. |
| `web/src/shell/routes.tsx` *(modify)* | Archived routes move under `/sessions`; `/archived*` become redirects. |
| `web/src/shell/AppShell.tsx` *(modify)* | `itemOpen` from `useOpenItem()`, not a bare `:id`. |
| `web/src/shell/navDefs.ts`, `useNavBadges.ts` *(modify)* | The `archived` section and badge are deleted. |
| `web/src/features/sessions/ScopeSegment.tsx` **(new)** | Two-link scope nav with counts. Tokens only. |
| `web/src/features/sessions/useArchivedListState.ts` **(new)** | Archived query + search + ordering, mirroring `useSessionListState`. |
| `web/src/features/sessions/SessionListColumn.tsx` *(modify)* | Head + scope body + foot; live and archived bodies as siblings. |

---

# PR 1 — Shared list chrome

Deliverable: `SessionList` and `ArchivedManager` render from one implementation of selection, bulk bar, checkbox row, skeleton, empty state and delete dialog. One visible change (noted in Task 5): the archived row's checkbox moves from inside the card to its left, matching the live list.

---

### Task 1: `useBulkSelection`

**Files:**
- Create: `web/src/components/list/useBulkSelection.ts`
- Test: `web/src/components/list/__tests__/useBulkSelection.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `useBulkSelection(visibleIds: readonly string[], presentIds?: readonly string[]): BulkSelection` where
  `BulkSelection = { selected: ReadonlySet<string>; count: number; isSelected(id): boolean; toggleOne(id, shift?): void; toggleAll(): void; clear(): void; allSelected: boolean; someSelected: boolean }`.

**Why two id lists.** `visibleIds` is the grouped/sorted/filtered order — it drives shift-ranges and select-all, because you can only range over what you can see. `presentIds` is the set a selection may survive in, and it defaults to `visibleIds`. `ArchivedManager` passes the *raw* archived list there so collapsing a group does not silently drop selections; `SessionList` omits it, keeping today's collapse-aware prune.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/components/list/__tests__/useBulkSelection.test.ts
import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useBulkSelection } from '../useBulkSelection'

const IDS = ['a', 'b', 'c', 'd']

describe('useBulkSelection', () => {
  it('toggles one id on and off, and tracks the count', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))
    expect(result.current.isSelected('b')).toBe(true)
    expect(result.current.count).toBe(1)
    act(() => result.current.toggleOne('b'))
    expect(result.current.isSelected('b')).toBe(false)
    expect(result.current.count).toBe(0)
  })

  it('shift-click selects the inclusive range from the anchor, in visible order', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))            // anchor
    act(() => result.current.toggleOne('d', true))      // range b..d
    expect([...result.current.selected].sort()).toEqual(['b', 'c', 'd'])
  })

  it('extends a range backwards from the same anchor', () => {
    // The anchor must NOT move on a shift-click, or a range cannot be re-extended
    // in the other direction without re-clicking the pivot.
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('c'))
    act(() => result.current.toggleOne('a', true))
    expect([...result.current.selected].sort()).toEqual(['a', 'b', 'c'])
  })

  it('falls back to a plain toggle when the anchor is no longer visible', () => {
    const { result, rerender } = renderHook(
      ({ visible }) => useBulkSelection(visible, IDS),
      { initialProps: { visible: IDS } },
    )
    act(() => result.current.toggleOne('a'))
    rerender({ visible: ['c', 'd'] })                   // 'a' scrolled out of the visible order
    act(() => result.current.toggleOne('d', true))
    expect([...result.current.selected].sort()).toEqual(['a', 'd'])
  })

  it('toggleAll selects every visible id, then clears when all are already selected', () => {
    const { result } = renderHook(() => useBulkSelection(['a', 'b'], IDS))
    act(() => result.current.toggleAll())
    expect([...result.current.selected].sort()).toEqual(['a', 'b'])
    expect(result.current.allSelected).toBe(true)
    act(() => result.current.toggleAll())
    expect(result.current.count).toBe(0)
  })

  it('reports someSelected without allSelected for a partial selection', () => {
    const { result } = renderHook(() => useBulkSelection(['a', 'b']))
    act(() => result.current.toggleOne('a'))
    expect(result.current.someSelected).toBe(true)
    expect(result.current.allSelected).toBe(false)
  })

  it('drops ids that leave `presentIds` (archived or deleted elsewhere)', () => {
    const { result, rerender } = renderHook(
      ({ present }) => useBulkSelection(present, present),
      { initialProps: { present: IDS } },
    )
    act(() => { result.current.toggleOne('a'); })
    act(() => { result.current.toggleOne('b'); })
    rerender({ present: ['b', 'c'] })
    expect([...result.current.selected]).toEqual(['b'])
  })

  it('keeps a selection that is merely hidden, when presentIds is wider than visibleIds', () => {
    // Collapsing a group must not lose the selection inside it.
    const { result, rerender } = renderHook(
      ({ visible }) => useBulkSelection(visible, IDS),
      { initialProps: { visible: IDS } },
    )
    act(() => result.current.toggleOne('a'))
    rerender({ visible: ['c', 'd'] })
    expect(result.current.isSelected('a')).toBe(true)
  })

  it('clear() empties the selection and forgets the anchor', () => {
    const { result } = renderHook(() => useBulkSelection(IDS))
    act(() => result.current.toggleOne('b'))
    act(() => result.current.clear())
    expect(result.current.count).toBe(0)
    // With no anchor, a shift-click is a plain toggle.
    act(() => result.current.toggleOne('d', true))
    expect([...result.current.selected]).toEqual(['d'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- useBulkSelection`
Expected: FAIL — `Failed to resolve import "../useBulkSelection"`.

- [ ] **Step 3: Write the implementation**

```ts
// web/src/components/list/useBulkSelection.ts
//
// Multi-select over a list: the selection set, the shift-range anchor, and the
// prune that drops ids which have left the list.
//
// `SessionList` and `ArchivedManager` each carried their own copy of all three
// (SessionList.tsx:158-197, ArchivedManager.tsx:252-299). They had already
// drifted in the one way that matters: which list a selection is allowed to
// survive in. This hook keeps BOTH lists explicit instead of picking a winner.
import { useState } from 'react'

export interface BulkSelection {
  selected: ReadonlySet<string>
  count: number
  isSelected: (id: string) => boolean
  /** Toggle one row. `shift` extends the range from the anchor over `visibleIds`. */
  toggleOne: (id: string, shift?: boolean) => void
  /** Select every visible id, or clear when they are all selected already. */
  toggleAll: () => void
  clear: () => void
  allSelected: boolean
  someSelected: boolean
}

/**
 * @param visibleIds the grouped/sorted/filtered order the user can see. Shift
 *   ranges and select-all operate over THIS — you can only select what you see.
 * @param presentIds the ids a selection may survive in; defaults to
 *   `visibleIds`. Pass the UNFILTERED list to keep a selection alive while its
 *   group is collapsed or a search hides it (ArchivedManager's behaviour);
 *   omit it to prune against the visible set (SessionList's).
 */
export function useBulkSelection(
  visibleIds: readonly string[],
  presentIds: readonly string[] = visibleIds,
): BulkSelection {
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  // Last row toggled WITHOUT shift — the pivot a range extends from.
  const [anchorId, setAnchorId] = useState<string | null>(null)

  // Prune at render time rather than in an effect: React re-renders
  // synchronously when state set during render differs from the current value,
  // and an effect here trips the set-state-in-effect lint rule. Both former
  // copies did it this way.
  if (selected.size > 0) {
    const present = new Set(presentIds)
    let changed = false
    const pruned = new Set<string>()
    for (const id of selected) {
      if (present.has(id)) pruned.add(id)
      else changed = true
    }
    if (changed) setSelected(pruned)
  }

  function toggleOne(id: string, shift = false) {
    const anchorIdx = anchorId === null ? -1 : visibleIds.indexOf(anchorId)
    if (shift && anchorIdx !== -1) {
      const end = visibleIds.indexOf(id)
      const [lo, hi] = anchorIdx < end ? [anchorIdx, end] : [end, anchorIdx]
      setSelected((prev) => {
        const next = new Set(prev)
        for (let i = lo; i <= hi; i++) next.add(visibleIds[i])
        return next
      })
      // The anchor deliberately stays put, so the range can be re-extended in
      // either direction without re-clicking the pivot.
      return
    }
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setAnchorId(id)
  }

  function toggleAll() {
    setSelected((prev) => {
      const every = visibleIds.length > 0 && visibleIds.every((id) => prev.has(id))
      return every ? new Set() : new Set(visibleIds)
    })
  }

  function clear() {
    setSelected(new Set())
    setAnchorId(null)
  }

  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id))
  const someSelected = visibleIds.some((id) => selected.has(id))

  return {
    selected,
    count: selected.size,
    isSelected: (id) => selected.has(id),
    toggleOne,
    toggleAll,
    clear,
    allSelected,
    someSelected,
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- useBulkSelection`
Expected: PASS — 9 tests.

- [ ] **Step 5: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/list/useBulkSelection.ts web/src/components/list/__tests__/useBulkSelection.test.ts
git commit -F - <<'MSG'
refactor(web): extract useBulkSelection — one selection/range/prune implementation

SessionList and ArchivedManager each carried their own copy. The hook keeps the
visible order (ranges, select-all) and the surviving set (prune) as separate
arguments, because the two lists legitimately differ on the second.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 2: `BulkActionBar` + `SelectableRow`

**Files:**
- Create: `web/src/components/list/BulkActionBar.tsx`, `web/src/components/list/SelectableRow.tsx`
- Test: `web/src/components/list/__tests__/BulkActionBar.test.tsx`, `web/src/components/list/__tests__/SelectableRow.test.tsx`

**Interfaces:**
- Consumes: nothing (`useBulkSelection` is wired in at the call sites, Task 5).
- Produces:
  - `<BulkActionBar label={string} clearLabel?={string} onClear={() => void}>{actions}</BulkActionBar>`
  - `<SelectableRow id={string} name={string} checked={boolean} onToggle={(id, shift) => void}>{row}</SelectableRow>`
  - `SELECT_CHECKBOX_CLS: string` (exported from `SelectableRow.tsx`)

**Why `children` and not props for the buttons.** The two bars share their shell (the region, its label, the count text, Clear) and share *nothing* about their actions: live has Archive / Archive-done, archived has Unarchive / Delete. Modelling the actions as props would mean a union of six optional callbacks where four are always undefined. The shell is the shared part; the actions are the caller's.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/components/list/__tests__/BulkActionBar.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BulkActionBar } from '../BulkActionBar'

describe('BulkActionBar', () => {
  it('is a labelled region carrying the count text and its actions', () => {
    render(
      <BulkActionBar label="3 selected" onClear={vi.fn()}>
        <button type="button">Unarchive (3)</button>
      </BulkActionBar>,
    )
    expect(screen.getByRole('region', { name: 'Bulk actions' })).toBeInTheDocument()
    expect(screen.getByText('3 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unarchive (3)' })).toBeInTheDocument()
  })

  it('calls onClear from the clear control, whose label the caller names', async () => {
    const onClear = vi.fn()
    render(<BulkActionBar label="0 selected" clearLabel="Done" onClear={onClear} />)
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onClear).toHaveBeenCalledOnce()
  })

  it('defaults the clear control to "Clear"', () => {
    render(<BulkActionBar label="1 selected" onClear={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument()
  })
})
```

```tsx
// web/src/components/list/__tests__/SelectableRow.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SelectableRow } from '../SelectableRow'

describe('SelectableRow', () => {
  it('names its checkbox after the row and reflects `checked`', () => {
    render(
      <SelectableRow id="s1" name="Work session" checked onToggle={vi.fn()}>
        <div>row body</div>
      </SelectableRow>,
    )
    const box = screen.getByRole('checkbox', { name: 'Select Work session' })
    expect(box).toBeChecked()
    expect(screen.getByText('row body')).toBeInTheDocument()
  })

  it('reports a plain click as shift=false', async () => {
    const onToggle = vi.fn()
    render(<SelectableRow id="s1" name="A" checked={false} onToggle={onToggle}>x</SelectableRow>)
    await userEvent.click(screen.getByRole('checkbox'))
    expect(onToggle).toHaveBeenCalledWith('s1', false)
  })

  it('reports a shift-click as shift=true', async () => {
    // A checkbox `change` event carries no modifier keys — only its `click`
    // does, and click always fires first. The row captures it there.
    const onToggle = vi.fn()
    render(<SelectableRow id="s1" name="A" checked={false} onToggle={onToggle}>x</SelectableRow>)
    const user = userEvent.setup()
    await user.keyboard('{Shift>}')
    await user.click(screen.getByRole('checkbox'))
    await user.keyboard('{/Shift}')
    expect(onToggle).toHaveBeenCalledWith('s1', true)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm --prefix web test -- components/list`
Expected: FAIL — both modules unresolved.

- [ ] **Step 3: Write the implementations**

```tsx
// web/src/components/list/BulkActionBar.tsx
//
// The bulk-action bar's SHELL — the labelled region, the count text and Clear.
// The actions are `children`: the live list offers Archive / Archive-done, the
// archived list Unarchive / Delete, and the two share none of them. Modelling
// both sets as props would be a union of six optionals with four always unset.
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'

interface BulkActionBarProps {
  /** The count phrase, e.g. "3 selected" or "Select sessions to archive". */
  label: string
  /** Text of the dismiss control. Live says "Done", archived says "Clear". */
  clearLabel?: string
  onClear: () => void
  children?: ReactNode
}

export function BulkActionBar({ label, clearLabel = 'Clear', onClear, children }: BulkActionBarProps) {
  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{label}</span>
      <div className="flex items-center gap-2 ml-auto flex-wrap">
        {children}
        <Button type="button" variant="secondary" onClick={onClear} className="min-h-[44px]">
          {clearLabel}
        </Button>
      </div>
    </div>
  )
}
```

```tsx
// web/src/components/list/SelectableRow.tsx
//
// A row with a selection checkbox in front of it.
//
// The shift-capture is the reason this is a component rather than a class
// constant: a checkbox's `change` event carries no modifier keys, but its
// `click` does, and click always fires first (even for a keyboard Space
// toggle). Both former copies re-derived that with their own ref.
import { useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

export const SELECT_CHECKBOX_CLS = cn(
  'h-4 w-4 flex-shrink-0 rounded border-gray-300 text-blue-600',
  'focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700',
  'dark:checked:bg-blue-500 cursor-pointer',
)

interface SelectableRowProps {
  id: string
  /** Row name — the checkbox is labelled "Select {name}". */
  name: string
  checked: boolean
  onToggle: (id: string, shift: boolean) => void
  children: ReactNode
}

export function SelectableRow({ id, name, checked, onToggle, children }: SelectableRowProps) {
  const shiftHeld = useRef(false)
  return (
    <div className="flex items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onClick={(e) => { shiftHeld.current = e.shiftKey }}
        onChange={() => onToggle(id, shiftHeld.current)}
        aria-label={`Select ${name}`}
        className={SELECT_CHECKBOX_CLS}
      />
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm --prefix web test -- components/list`
Expected: PASS — 7 tests across the two new files plus Task 1's 9.

- [ ] **Step 5: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/list/BulkActionBar.tsx web/src/components/list/SelectableRow.tsx web/src/components/list/__tests__/
git commit -F - <<'MSG'
refactor(web): shared BulkActionBar shell and SelectableRow

Both lists had their own copy of the bar's markup and of the click-before-change
shift capture, plus two spellings of one checkbox class constant.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 3: `SkeletonCard` + `NoMatchState`

**Files:**
- Create: `web/src/components/list/SkeletonCard.tsx`, `web/src/components/list/NoMatchState.tsx`
- Test: `web/src/components/list/__tests__/NoMatchState.test.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: `<SkeletonCard />`; `<NoMatchState message={string} actionLabel={string} onClear={() => void} />`.

`SkeletonCard` is a verbatim move with no new test — `SessionList.test.tsx`'s loading assertions already cover it and keep passing through Task 5.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/list/__tests__/NoMatchState.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NoMatchState } from '../NoMatchState'

describe('NoMatchState', () => {
  it('announces the reason as a status and offers the caller-named reset', async () => {
    const onClear = vi.fn()
    render(
      <NoMatchState
        message="No archived sessions match your search."
        actionLabel="Clear"
        onClear={onClear}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('No archived sessions match your search.')
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onClear).toHaveBeenCalledOnce()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- NoMatchState`
Expected: FAIL — module unresolved.

- [ ] **Step 3: Write the implementations**

```tsx
// web/src/components/list/SkeletonCard.tsx
//
// Moved verbatim from SessionList.tsx. Kept as its own module so the archived
// list can adopt the same loading shape rather than growing a second one.
import { cn } from '@/lib/utils'

export function SkeletonCard() {
  return (
    <div
      className={cn(
        'rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 p-3 min-h-[44px] animate-pulse',
      )}
      aria-hidden="true"
    >
      <div className="flex items-center gap-3">
        <div className="flex-1 space-y-2">
          <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/3" />
          <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded w-1/2" />
        </div>
        <div className="h-8 w-8 bg-gray-200 dark:bg-gray-700 rounded" />
      </div>
    </div>
  )
}
```

```tsx
// web/src/components/list/NoMatchState.tsx
//
// "The list is empty because you narrowed it" — as distinct from "you have
// none", which is `EmptyState`. Conflating the two is how a filtered list came
// to invite the user to create their first session.
//
// `role="status"` so the reason is announced rather than silently replacing the
// rows; the reset is the caller's, because only the caller knows what it has to
// undo (a search, a search AND filters, a scope).
import { cn } from '@/lib/utils'

interface NoMatchStateProps {
  message: string
  actionLabel: string
  onClear: () => void
}

export function NoMatchState({ message, actionLabel, onClear }: NoMatchStateProps) {
  return (
    <div
      className="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 p-6 text-center"
      role="status"
    >
      <p className="text-sm text-gray-500 dark:text-gray-400">{message}</p>
      <button
        type="button"
        onClick={onClear}
        className={cn(
          'mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        )}
      >
        {actionLabel}
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- NoMatchState`
Expected: PASS — 1 test.

- [ ] **Step 5: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/list/SkeletonCard.tsx web/src/components/list/NoMatchState.tsx web/src/components/list/__tests__/NoMatchState.test.tsx
git commit -F - <<'MSG'
refactor(web): shared SkeletonCard and NoMatchState for list surfaces

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 4: One `PermanentDeleteDialog`

**Files:**
- Modify: `web/src/components/PermanentDeleteDialog.tsx`
- Modify: `web/src/shell/AppShell.tsx:503-506`, `web/src/components/session/SessionActions.tsx:211-216`
- Test: `web/src/components/__tests__/PermanentDeleteDialog.test.tsx` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `<PermanentDeleteDialog names={string[]} onClose onConfirm isPending progress?={{done:number;total:number}} onCancel?={() => void} />`. `names.length === 1` renders the single-session copy; more renders the bulk copy. `progress` + `onCancel` are the bulk path's progress bar and abort.

The shared component gains the archived copy's `dismissable={!isPending}`: a modal that Esc-closes mid-bulk-delete abandons a run the user cannot see the end of. For the single-session path this changes nothing except during the in-flight moment.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/PermanentDeleteDialog.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PermanentDeleteDialog } from '../PermanentDeleteDialog'

const base = { onClose: vi.fn(), onConfirm: vi.fn(), isPending: false }

describe('PermanentDeleteDialog', () => {
  it('names the single session in the title and the body', () => {
    render(<PermanentDeleteDialog {...base} names={['My Session']} />)
    expect(screen.getByText('Permanently delete session')).toBeInTheDocument()
    expect(screen.getByText(/My Session/)).toBeInTheDocument()
  })

  it('counts the sessions when several are named', () => {
    render(<PermanentDeleteDialog {...base} names={['a', 'b', 'c', 'd']} />)
    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()
  })

  it('starts focus on Cancel and closes from it', async () => {
    const onClose = vi.fn()
    render(<PermanentDeleteDialog {...base} onClose={onClose} names={['One']} />)
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    expect(cancel).toHaveFocus()
    await userEvent.click(cancel)
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('shows a progressbar and routes Cancel to the abort while a bulk run is in flight', async () => {
    const onCancel = vi.fn()
    const onClose = vi.fn()
    render(
      <PermanentDeleteDialog
        {...base}
        names={['a', 'b', 'c']}
        isPending
        progress={{ done: 1, total: 3 }}
        onCancel={onCancel}
        onClose={onClose}
      />,
    )
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '1')
    expect(bar).toHaveAttribute('aria-valuemax', '3')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledOnce()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('confirms', async () => {
    const onConfirm = vi.fn()
    render(<PermanentDeleteDialog {...base} onConfirm={onConfirm} names={['One']} />)
    await userEvent.click(screen.getByRole('button', { name: 'Delete permanently' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- PermanentDeleteDialog`
Expected: FAIL — `names` is not a prop; the component requires `sessionName`.

- [ ] **Step 3: Rewrite the component**

Replace the whole of `web/src/components/PermanentDeleteDialog.tsx`:

```tsx
/**
 * PermanentDeleteDialog — the destructive "this is gone for good" confirm, for
 * one session or many.
 *
 * There were two of these: this one (single session; AppShell's card menu and
 * the in-session SessionActions) and a private copy inside ArchivedManager that
 * had grown the bulk copy, a progress bar and a cancel. One dialog now, because
 * the difference between them was never the dialog — it was how many names the
 * caller had.
 *
 * Focus starts on Cancel. While a bulk run is in flight the dialog refuses to
 * dismiss and Cancel becomes the abort: Esc-closing mid-run abandons a delete
 * the user can no longer watch.
 */
import { useRef } from 'react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { prefersReducedMotion } from './session/brailleSpinner'

export interface BulkDeleteProgressShape {
  done: number
  total: number
}

interface PermanentDeleteDialogProps {
  /** One name = the single-session copy; more = the counted bulk copy. */
  names: string[]
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
  /** Bulk only: drives the progress bar. */
  progress?: BulkDeleteProgressShape | null
  /** Bulk only: abort at the next chunk boundary. */
  onCancel?: () => void
}

export function PermanentDeleteDialog({
  names, onClose, onConfirm, isPending, progress, onCancel,
}: PermanentDeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const count = names.length
  const title = count === 1 ? 'Permanently delete session' : `Permanently delete ${count} sessions`

  return (
    <Dialog open onClose={onClose} title={title} size="sm" initialFocus={cancelRef} dismissable={!isPending}>
      {isPending && progress ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm text-ink-muted">
            <span>Deleting…</span>
            <span className="tabular-nums">{progress.done} of {progress.total}</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.done}
            aria-valuetext={`Deleting ${progress.done} of ${progress.total}`}
            className="h-2 w-full overflow-hidden rounded bg-surface-raised"
          >
            <div
              className={cn('h-full rounded bg-danger-strong', !prefersReducedMotion() && 'transition-[width] duration-200 ease-linear')}
              style={{ width: `${progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0}%` }}
            />
          </div>
        </div>
      ) : (
        <p className="text-sm text-ink-muted">
          {count === 1 ? (
            <>
              Permanently delete <span className="font-medium text-ink">&lsquo;{names[0]}&rsquo;</span>? This removes the
              session folder and all its data. This can&rsquo;t be undone.
            </>
          ) : (
            <>
              Permanently delete <span className="font-medium text-ink">{count} sessions</span>? This removes their
              session folders and all their data. This can&rsquo;t be undone.
            </>
          )}
        </p>
      )}

      <div className="flex gap-3 justify-end pt-2">
        <Button
          ref={cancelRef}
          type="button"
          variant="secondary"
          onClick={isPending ? onCancel : onClose}
          disabled={isPending && !onCancel}
        >
          Cancel
        </Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete permanently'}
        </Button>
      </div>
    </Dialog>
  )
}
```

- [ ] **Step 4: Update the two live call sites**

In `web/src/shell/AppShell.tsx`, change `sessionName={deleteTarget.name}` to `names={[deleteTarget.name]}`.
In `web/src/components/session/SessionActions.tsx`, change `sessionName={sessionName}` to `names={[sessionName]}`.

- [ ] **Step 5: Run the tests**

Run: `npm --prefix web test -- PermanentDeleteDialog SessionActions AppShell`
Expected: PASS — the new file's 5 tests, and `SessionActions.test.tsx` / `AppShell.test.tsx` unchanged and green.

- [ ] **Step 6: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/PermanentDeleteDialog.tsx web/src/components/__tests__/PermanentDeleteDialog.test.tsx web/src/shell/AppShell.tsx web/src/components/session/SessionActions.tsx
git commit -F - <<'MSG'
refactor(web): one PermanentDeleteDialog for single and bulk deletes

ArchivedManager's private copy had grown the bulk copy, the progress bar and the
cancel; the shared one had none of them. The dialog now takes `names: string[]`
and optional progress, and refuses to dismiss while a bulk run is in flight.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 5: Rewrite `SessionList` and `ArchivedManager` onto the shared chrome

**Files:**
- Modify: `web/src/components/SessionList.tsx` (delete lines 12–72 and 155–197, rewire the render)
- Modify: `web/src/components/ArchivedManager.tsx` (delete lines 36, 52–121, 179–216, 252–299, and both empty states)
- Test: `web/src/components/__tests__/SessionList.test.tsx`, `ArchivedTab.test.tsx`, `ArchivedManager.test.tsx` (all existing — they are the regression net and must pass **unmodified**, with the one exception in Step 4)

**Interfaces:**
- Consumes: `useBulkSelection`, `BulkActionBar`, `SelectableRow`, `SELECT_CHECKBOX_CLS`, `SkeletonCard`, `NoMatchState`, `PermanentDeleteDialog` (Tasks 1–4).
- Produces: no new exports. `SessionList` and `ArchivedManager` keep their current props exactly.

**The one visible change.** `ArchivedManager`'s row checkbox currently sits *inside* the card body (`ArchivedManager.tsx:381-389`, `mt-0.5`). Routed through `SelectableRow` it moves to the left of the card, which is where the live list has always put it. The checkbox keeps its `aria-label="Select {name}"`, so every existing assertion still resolves — this is a layout change, not a semantic one, and PR 2 makes both lists identical anyway.

- [ ] **Step 1: Run the three suites to record the green baseline**

Run: `npm --prefix web test -- SessionList ArchivedTab ArchivedManager`
Expected: PASS. Note the test counts — the same counts must pass at Step 5.

- [ ] **Step 2: Rewrite `SessionList`**

Delete `CHECKBOX_CLS` (line 12), the whole `BulkActionBarProps` interface + `BulkActionBar` function (lines 18–72), the local `SkeletonCard` (lines 97–118), the render-time prune + `toggleOne` + `clearSelection` (lines 155–197 — keep `clearSelection`'s extra job of leaving select mode), and the inline dashed panel (lines 240–262).

Replace the imports and the affected bodies:

```tsx
import { useState } from 'react'
import { Archive, RotateCcw } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAutomations, useFixPr, useGenerateSuggestions, useSetAutopilotPaused, useBulkArchiveSessions, useResumeInterrupted } from '@/api/hooks'
import { SessionCard } from './SessionCard'
import { GroupedList } from './GroupedList'
import { EmptyState } from './EmptyState'
import { BulkActionBar } from './list/BulkActionBar'
import { NoMatchState } from './list/NoMatchState'
import { SelectableRow } from './list/SelectableRow'
import { SkeletonCard } from './list/SkeletonCard'
import { useBulkSelection } from './list/useBulkSelection'
import { Button } from '@/ui/Button'
import { ARCHIVABLE_STATUSES, type Session, type Account } from '@/api/types'
import type { SessionGroup } from '@/session/ordering'
```

Inside the component, replace the selection state block with:

```tsx
  const [selectMode, setSelectMode] = useState(false)
  // The prune list is the visible one: SessionList is handed `orderedSessions`,
  // which is already collapse-aware, and that is the set today's behaviour
  // prunes against.
  const selection = useBulkSelection(sessions.map((s) => s.id))

  function clearSelection() {
    selection.clear()
    setSelectMode(false)
  }

  function handleBulkArchive() {
    const ids = [...selection.selected]
    if (ids.length === 0) return
    bulkArchive.mutate(ids, { onSuccess: () => clearSelection() })
  }
```

`handleArchiveDone` is unchanged. Replace the loading branch's three inline skeletons with the imported `SkeletonCard` (same JSX, now the imported component). Replace the `isFiltered` branch with:

```tsx
    if (isFiltered) {
      return (
        <NoMatchState
          message="No sessions match your search or filters."
          actionLabel="Clear search and filters"
          onClear={() => onClearSearch?.()}
        />
      )
    }
```

Replace the `renderCard` select branch:

```tsx
    if (!selectMode) return <div key={session.id}>{card}</div>

    return (
      <SelectableRow
        key={session.id}
        id={session.id}
        name={session.name}
        checked={selection.isSelected(session.id)}
        onToggle={selection.toggleOne}
      >
        {card}
      </SelectableRow>
    )
```

And the bar:

```tsx
      {selectMode && (
        <BulkActionBar
          label={selection.count > 0 ? `${selection.count} selected` : 'Select sessions to archive'}
          clearLabel="Done"
          onClear={clearSelection}
        >
          {archivableDoneCount > 0 && (
            <Button
              type="button"
              variant="secondary"
              onClick={handleArchiveDone}
              disabled={bulkArchive.isPending}
              className="min-h-[44px] text-xs"
            >
              <Archive size={14} className="mr-1" aria-hidden="true" />
              Archive done ({archivableDoneCount})
            </Button>
          )}
          {selection.count > 0 && (
            <Button
              type="button"
              onClick={handleBulkArchive}
              disabled={bulkArchive.isPending}
              className={cn(
                'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
                'dark:bg-blue-700 dark:hover:bg-blue-600',
              )}
            >
              {bulkArchive.isPending ? 'Archiving…' : `Archive (${selection.count})`}
            </Button>
          )}
        </BulkActionBar>
      )}
```

- [ ] **Step 3: Rewrite `ArchivedManager`**

Delete `CHECKBOX_CLS` (line 36), the inline `PermanentDeleteDialogProps` + `PermanentDeleteDialog` (lines 52–121), the inline `BulkActionBarProps` + `BulkActionBar` (lines 179–216), the render-time prune + `toggleOne` + `toggleAll` + `clearSelection` (lines 252–299), the `selectAllRef`/`allSelected`/`someSelected`/`useEffect` block, and both empty-state JSX blocks.

Import from the shared modules and the top-level dialog:

```tsx
import { PermanentDeleteDialog } from './PermanentDeleteDialog'
import { BulkActionBar } from './list/BulkActionBar'
import { NoMatchState } from './list/NoMatchState'
import { SelectableRow } from './list/SelectableRow'
import { SELECT_CHECKBOX_CLS } from './list/SelectableRow'
import { useBulkSelection } from './list/useBulkSelection'
```

Wire the selection — note the second argument, which is why the hook takes one:

```tsx
  // Range + select-all follow the VISIBLE order; a selection survives in the
  // RAW list, so collapsing a group or typing in the search box does not
  // silently drop rows the user has already ticked.
  const selection = useBulkSelection(
    visible.map((s) => s.id),
    archivedSessions.map((s) => s.id),
  )
```

Replace every `selected.has(x)` with `selection.isSelected(x)`, `selected.size` with `selection.count`, `setSelected(new Set())` with `selection.clear()`, and `[...selected]` with `[...selection.selected]`. In `openBulkDelete`, `selected.has` becomes `selection.isSelected`.

The select-all header keeps its indeterminate wiring but reads the hook:

```tsx
  const selectAllRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selection.someSelected && !selection.allSelected
    }
  }, [selection.someSelected, selection.allSelected])
```

…and its `<input>` uses `checked={selection.allSelected}`, `onChange={selection.toggleAll}`, `className={SELECT_CHECKBOX_CLS}`.

`renderArchivedRow` loses its checkbox and wraps in `SelectableRow`:

```tsx
  function renderArchivedRow(s: ArchivedSession) {
    const displayName = displayNameOf(s)
    const archivedDate = new Date(s.archivedAt).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
    return (
      <SelectableRow
        key={s.id}
        id={s.id}
        name={displayName}
        checked={selection.isSelected(s.id)}
        onToggle={selection.toggleOne}
      >
        <div
          className={cn(
            'flex flex-col gap-2 rounded-lg border border-gray-200 dark:border-gray-700',
            'bg-white dark:bg-gray-800 px-3 py-2',
          )}
        >
          {/* …the existing name/account/date block and the three action
              buttons, verbatim, minus the checkbox and its wrapper div… */}
        </div>
      </SelectableRow>
    )
  }
```

Both empty states become:

```tsx
      {archivedSessions.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
          No archived sessions.
        </p>
      )}

      {archivedSessions.length > 0 && search.isFiltered && !hasFilteredSessions && (
        <NoMatchState
          message="No archived sessions match your search."
          actionLabel="Clear"
          onClear={() => search.setQuery('')}
        />
      )}
```

The bulk bar:

```tsx
      {selection.count > 0 && (
        <BulkActionBar label={`${selection.count} selected`} onClear={selection.clear}>
          <Button
            type="button"
            onClick={handleBulkUnarchive}
            disabled={bulkUnarchive.isPending}
            className={cn(
              'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
            )}
          >
            {bulkUnarchive.isPending ? 'Unarchiving…' : `Unarchive (${selection.count})`}
          </Button>
          <Button
            type="button"
            onClick={openBulkDelete}
            className={cn(
              'min-h-[44px] bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600 focus-visible:ring-red-500',
            )}
          >
            Delete ({selection.count})
          </Button>
        </BulkActionBar>
      )}
```

Both `<PermanentDeleteDialog>` usages already pass `names`, so they now resolve to the shared component with no prop change.

- [ ] **Step 4: Adjust the one assertion that pins the deleted markup**

`ArchivedManager.test.tsx` asserts the *clear* control on the no-match state by its exact class-derived look in one place only if it queries by anything other than role/name — check it. If any assertion reaches for the old inline `<p>` empty state's container rather than its text, retarget it at `screen.getByRole('status')`. Change nothing else: every other assertion must pass untouched, and an assertion you had to weaken is a regression you are hiding.

- [ ] **Step 5: Run the three suites and confirm the same counts**

Run: `npm --prefix web test -- SessionList ArchivedTab ArchivedManager`
Expected: PASS, with the counts recorded in Step 1.

- [ ] **Step 6: Run the FULL suite and the gates, then commit**

The full run matters here: `SessionList` is mounted by suites that hand-mock `@/api/hooks`, and this task changes its import graph.

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/SessionList.tsx web/src/components/ArchivedManager.tsx web/src/components/__tests__/
git commit -F - <<'MSG'
refactor(web): route both session lists through the shared list chrome

SessionList and ArchivedManager now share one selection hook, one bulk-action
bar, one checkbox row, one skeleton, one no-match state and one delete dialog.
Every duplicated copy is deleted in this commit.

The archived row's checkbox moves to the left of the card, where the live list
has always had it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

**PR 1 ends here.** Open it, mark ready, arm auto-merge. Nothing user-visible changed except the archived checkbox position.

---

# PR 2 — Shared card chrome, the archived card, and the `archivedAt` fix

Deliverable: archived rows are session cards. `ArchivedManager` is gone.

---

### Task 6: `CardFrame`

**Files:**
- Create: `web/src/components/card/CardFrame.tsx`
- Test: `web/src/components/card/__tests__/CardFrame.test.tsx`

**Interfaces:**
- Consumes: `useSwipeAction` (`@/lib/useSwipeAction` → `{ dx, openSide, dragging, onPointerDown, reset, consumeDrag }`), `useMediaQuery`, `cn`.
- Produces:

```ts
export interface CardSwipeAction {
  /** Accessible name of the revealed button, e.g. "Archive Alpha". */
  label: string
  icon: ReactNode
  onCommit: () => void
  /** Existing testids must be preserved: 'swipe-panel-archive' / 'swipe-panel-delete'. */
  testId: string
  /** Panel colour classes. */
  className: string
}

export interface CardFrameProps {
  id: string
  /** Accessible name of the open control, e.g. "Open session Alpha". */
  openLabel: string
  /** Left-edge status rail colour class. */
  railClass: string
  selected: boolean
  listMode: boolean
  onOpen: (id: string) => void
  /** Row-level key handling (each card owns its own shortcut table). */
  onKeyDown?: (e: React.KeyboardEvent) => void
  /** Swipe right reveals this (reversible action). Omit to disable the edge. */
  swipeRight?: CardSwipeAction
  /** Swipe left reveals this (destructive). Omit to disable the edge. */
  swipeLeft?: CardSwipeAction
  /** Disable the gesture entirely (a pending row). */
  swipeEnabled?: boolean
  /** Rendered before the open button on line 1 (the sub-agent chevron). */
  leading?: ReactNode
  /** Passive content INSIDE the open button (dot, markers, title). */
  identity: ReactNode
  /** Interactive siblings after the open button (chips, actions, the ⋯ menu). */
  trailing?: ReactNode
  /** Line 2 and anything below it. */
  children?: ReactNode
}
```

**What the boundary is.** `CardFrame` owns everything that is true of *a row in this list*: the article, the rail, the swipe machinery, the selected ring, the scroll-into-view, the list-mode focus, and the open button with its swipe guards. It owns none of what a row *says*. `SessionCard`'s `pending` early-return stays in `SessionCard` — an archived row is never pending, and a "pending" flag on the frame would be a field only one caller can ever set.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/card/__tests__/CardFrame.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CardFrame } from '../CardFrame'

function props(over = {}) {
  return {
    id: 's1',
    openLabel: 'Open session Alpha',
    railClass: 'bg-success',
    selected: false,
    listMode: false,
    onOpen: vi.fn(),
    identity: <span>Alpha</span>,
    ...over,
  }
}

function fg() {
  const el = screen.getByTestId('card-swipe-fg')
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 }) // commit at 135px
  return el
}

describe('CardFrame', () => {
  it('paints the rail with the class it is given', () => {
    render(<CardFrame {...props()} />)
    expect(screen.getByTestId('card-status-rail').className).toContain('bg-success')
  })

  it('opens from the open control', () => {
    const onOpen = vi.fn()
    render(<CardFrame {...props({ onOpen })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open session Alpha' }))
    expect(onOpen).toHaveBeenCalledWith('s1')
  })

  it('opens from a click on passive card area, but not from a click on a trailing button', () => {
    const onOpen = vi.fn()
    const onAction = vi.fn()
    render(
      <CardFrame
        {...props({ onOpen })}
        trailing={<button type="button" onClick={onAction}>act</button>}
      >
        <div data-testid="line2">context</div>
      </CardFrame>,
    )
    fireEvent.click(screen.getByTestId('line2'))
    expect(onOpen).toHaveBeenCalledWith('s1')
    onOpen.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'act' }))
    expect(onAction).toHaveBeenCalledOnce()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('commits the right-swipe action', () => {
    const onCommit = vi.fn()
    render(
      <CardFrame
        {...props()}
        swipeRight={{ label: 'Archive Alpha', icon: <i />, onCommit, testId: 'swipe-panel-archive', className: 'bg-gray-200' }}
      />,
    )
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 })   // dx +160
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('reveals a tappable button on a short left-swipe rather than committing', () => {
    const onCommit = vi.fn()
    render(
      <CardFrame
        {...props()}
        swipeLeft={{ label: 'Delete Alpha', icon: <i />, onCommit, testId: 'swipe-panel-delete', className: 'bg-red-600' }}
      />,
    )
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 })   // dx -80: reveal only
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(onCommit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alpha' }))
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('mounts no swipe edge that was not configured', () => {
    render(<CardFrame {...props()} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 })
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(screen.queryByTestId('swipe-panel-delete')).not.toBeInTheDocument()
  })

  it('opens on Enter when not in list mode, and defers keys to onKeyDown when it is', () => {
    const onOpen = vi.fn()
    const onKeyDown = vi.fn()
    const { rerender } = render(<CardFrame {...props({ onOpen })} />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Open session Alpha' }), { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledWith('s1')

    onOpen.mockClear()
    rerender(<CardFrame {...props({ onOpen, listMode: true, onKeyDown })} />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Open session Alpha' }), { key: 'x' })
    expect(onKeyDown).toHaveBeenCalledOnce()
  })

  it('gives the selected row the focusable tabindex and the ring', () => {
    render(<CardFrame {...props({ selected: true })} />)
    expect(screen.getByRole('button', { name: 'Open session Alpha' })).toHaveAttribute('tabindex', '0')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- CardFrame`
Expected: FAIL — module unresolved.

- [ ] **Step 3: Write `CardFrame`**

Move the machinery out of `SessionCard.tsx` verbatim — the `<article>` classes (lines ~305–333), the rail span, the two swipe panels, the foreground wrapper, `handleCardClick`, the `scrollIntoView` effect, the `listMode` focus effect, and the open `<button>` (with its `id={`row-${id}`}`, `tabIndex`, `data-selected` and swipe guards). Do not retype the class strings; copy them.

```tsx
// web/src/components/card/CardFrame.tsx
//
// The frame every row in the sessions list shares: the article, the status
// rail, the swipe edges and their foreground, the selection ring, the
// scroll-into-view, the list-mode focus, and the open control.
//
// It was inlined in SessionCard, which meant an archived row could either grow
// a second copy of all of it or go without — and it went without: no rail, no
// swipe, no keyboard row, a bare div. The split is by WHAT IS TRUE OF A ROW
// (here) versus WHAT A ROW SAYS (the card): `identity` goes inside the open
// button because it is passive, `trailing` sits outside it because chips and
// menus are interactive and must not nest inside a button.
//
// SessionCard's `pending` early-return deliberately stays in SessionCard: an
// archived row is never pending, and a flag only one caller can set is not a
// property of the frame.
import { useEffect, useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useSwipeAction } from '@/lib/useSwipeAction'

export interface CardSwipeAction {
  label: string
  icon: ReactNode
  onCommit: () => void
  testId: string
  className: string
}

export interface CardFrameProps {
  id: string
  openLabel: string
  railClass: string
  selected: boolean
  listMode: boolean
  onOpen: (id: string) => void
  onKeyDown?: (e: React.KeyboardEvent) => void
  swipeRight?: CardSwipeAction
  swipeLeft?: CardSwipeAction
  swipeEnabled?: boolean
  leading?: ReactNode
  identity: ReactNode
  trailing?: ReactNode
  children?: ReactNode
}

/** Width of a revealed edge panel. */
const PANEL_PX = 88

export function CardFrame({
  id, openLabel, railClass, selected, listMode, onOpen, onKeyDown,
  swipeRight, swipeLeft, swipeEnabled = true, leading, identity, trailing, children,
}: CardFrameProps) {
  const articleRef = useRef<HTMLElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // lg = the app's desktop breakpoint. jsdom reports false → mobile, so tests
  // exercise the gesture branch by default.
  const isWide = useMediaQuery('(min-width: 1024px)')
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const gestureLive = swipeEnabled && !isWide && (!!swipeLeft || !!swipeRight)
  const swipe = useSwipeAction({
    enabled: gestureLive,
    onCommit: (side) => (side === 'left' ? swipeLeft?.onCommit() : swipeRight?.onCommit()),
  })
  const restX = swipe.dragging
    ? swipe.dx
    : swipe.openSide === 'left' ? -PANEL_PX : swipe.openSide === 'right' ? PANEL_PX : 0
  const panelsMounted = gestureLive && (swipe.dragging || swipe.openSide !== null)

  // jsdom does not implement scrollIntoView.
  useEffect(() => {
    if (selected && articleRef.current?.scrollIntoView) {
      articleRef.current.scrollIntoView({ block: 'nearest' })
    }
  }, [selected])

  // In list-focus mode the highlighted row takes REAL focus, so single-key
  // shortcuts land here and never type into the prompt.
  useEffect(() => {
    if (listMode && selected) buttonRef.current?.focus()
  }, [listMode, selected])

  /** Whole-card click-to-open. A click on or inside an interactive descendant
   *  is that descendant's; everything else opens the row. */
  function handleCardClick(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest('button, a, [role="menu"]')) return
    if (swipe.openSide) { swipe.reset(); return }
    if (swipe.consumeDrag()) return
    onOpen(id)
  }

  function handleOpenKeyDown(e: React.KeyboardEvent) {
    if (!listMode) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        onOpen(id)
      }
      return
    }
    onKeyDown?.(e)
  }

  function renderPanel(side: 'left' | 'right', action: CardSwipeAction) {
    const revealed = swipe.openSide === side
    return (
      <button
        type="button"
        data-testid={action.testId}
        aria-label={action.label}
        // Hidden from assistive tech until revealed, so a screen reader does not
        // announce a phantom action behind every card.
        aria-hidden={!revealed}
        tabIndex={revealed ? 0 : -1}
        onClick={() => { swipe.reset(); action.onCommit() }}
        className={cn(
          'absolute inset-y-0 flex w-[88px] items-center justify-center',
          side === 'right' ? 'left-0' : 'right-0',
          action.className,
          revealed ? 'pointer-events-auto' : 'pointer-events-none',
        )}
      >
        {action.icon}
      </button>
    )
  }

  return (
    <article
      ref={articleRef}
      onClick={handleCardClick}
      className={cn(
        // `@container` makes the card its own container-query context so the
        // layout responds to the CARD's width (the drag-resizable sidebar), not
        // the viewport. Gated variants must stay ≤ @md — the content box tops
        // out near 503px, so @lg can never match inside the sidebar.
        'group @container relative flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px] cursor-pointer',
        'focus-within:ring-2 focus-within:ring-blue-500',
        selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
        // Clip the edge panels only while they are mounted. A permanent
        // overflow-hidden also clipped the ⋯ menu on wide touch devices.
        panelsMounted && 'overflow-hidden',
      )}
    >
      {/* Status rail — outside the swipe foreground, so triage colour holds
          while the card is being dragged. */}
      <span
        data-testid="card-status-rail"
        aria-hidden="true"
        className={cn('absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full', railClass)}
      />

      {panelsMounted && (
        <>
          {swipeRight && renderPanel('right', swipeRight)}
          {swipeLeft && renderPanel('left', swipeLeft)}
        </>
      )}

      <div
        {...(gestureLive ? { 'data-testid': 'card-swipe-fg', onPointerDown: swipe.onPointerDown } : {})}
        style={(gestureLive && restX !== 0) ? { transform: `translateX(${restX}px)` } : undefined}
        className={cn(
          gestureLive && 'relative bg-white dark:bg-gray-800 touch-pan-y',
          gestureLive && !swipe.dragging && !reduceMotion && 'transition-transform duration-200',
        )}
      >
        <div data-testid="card-identity" className="flex items-center gap-1.5 min-w-0">
          {leading}
          {/* `id` is the target of DispatchBar's aria-activedescendant: it
              signals the highlighted row without moving DOM focus.
              `aria-selected` is omitted on a plain button — axe rejects it
              without role="option". */}
          <button
            type="button"
            id={`row-${id}`}
            ref={buttonRef}
            tabIndex={selected ? 0 : -1}
            data-selected={selected}
            className={cn(
              'flex-1 min-w-0 flex items-center gap-1.5 text-left py-0.5',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
            )}
            onClick={(e) => {
              // The article handler skips clicks on buttons — this IS the open
              // control, so it acts directly, with the same swipe guards.
              e.stopPropagation()
              if (swipe.openSide) { swipe.reset(); return }
              if (swipe.consumeDrag()) return
              onOpen(id)
            }}
            onKeyDown={handleOpenKeyDown}
            aria-label={openLabel}
          >
            {identity}
          </button>
          {trailing}
        </div>
        {children}
      </div>
    </article>
  )
}
```

Note: the frame takes no `name`. Every accessible name it renders is handed to it fully formed — `openLabel` for the open control, `CardSwipeAction.label` for each edge — because only the caller knows whether the row is a "session" or an archived one, and a frame that rebuilt those strings would have to know too.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- CardFrame`
Expected: PASS — 8 tests.

- [ ] **Step 5: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/card/
git commit -F - <<'MSG'
refactor(web): extract CardFrame — the row frame both card kinds need

The article, status rail, swipe edges, selection ring, scroll-into-view,
list-mode focus and open control were inlined in SessionCard, so the archived
row went without all of them.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 7: `CardMenu`, and `SessionCard` onto the shared frame

**Files:**
- Create: `web/src/components/card/CardMenu.tsx`
- Modify: `web/src/components/SessionCard.tsx`
- Test: the 12 existing `SessionCard*.test.tsx` files are the net — they must pass **unmodified**.

**Interfaces:**
- Consumes: `CardFrame` (Task 6), `pushOverlay`/`popOverlay` from `@/shell/useOverlayState`.
- Produces: `<CardMenu label={string}>{(close) => items}</CardMenu>`, plus `menuItem: string`, `actionBtn: string` and `Sep` re-exported from `card/CardMenu.tsx`.

**Why a render prop.** Every item must close the menu before acting. Passing `close` down is one contract; the alternative — each caller threading its own `setMenuOpen` — is the state duplication the extraction exists to remove.

- [ ] **Step 1: Write `CardMenu`**

Move verbatim from `SessionCard.tsx`: the `actionBtn` and `menuItem` class constants, the `Sep` component, the outside-click effect, the overlay push/pop effect, the focus-the-trigger effect, the container `onKeyDown` Escape handler, the trigger button and the `role="menu"` popup div.

```tsx
// web/src/components/card/CardMenu.tsx
//
// The ⋯ overflow menu on a card row: the trigger, the popup, and the three
// behaviours that make it behave like a menu rather than a div that appears.
//
//  · Outside-click closes it (mirrors SessionActions).
//  · Escape closes it, handled on the CONTAINER so it works whether focus is on
//    the trigger or inside the list, and it stopPropagation()s — dismissing a
//    menu must not also detach from the session.
//  · An open menu is an OVERLAY: it registers with shell/useOverlayState so the
//    shell's shortcuts (↑/↓ over the list, ←/Esc out of a session) do not fire
//    through it, the same owner Dialog, Sheet and ui/Menu register with.
//
// The trigger takes focus when the menu opens. On macOS Safari and Firefox a
// click does not move focus to a <button>, and keydown goes to
// document.activeElement — so without this the Escape handler never fires for a
// mouse-opened menu while the overlay push keeps every global shortcut
// suppressed, and the keyboard goes silently inert.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreVertical } from 'lucide-react'
import { cn } from '@/lib/utils'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'

/** Shared sizing for the per-row icon buttons: 44px tap target on touch, a
 *  denser 32px on hover-capable pointers. */
export const actionBtn = cn(
  'h-11 w-11 [@media(hover:hover)]:h-8 [@media(hover:hover)]:w-8',
  'flex items-center justify-center rounded-md flex-shrink-0',
  'focus-visible:outline-none focus-visible:ring-2',
  // Press feedback: hover gives no change once the cursor is already over the
  // button, and touch has no hover at all. active: fires on both.
  'transition-[background-color,transform] duration-100',
  'active:scale-90 active:duration-75',
)

/** Styling shared by every entry of a card's ⋯ menu. */
export const menuItem = cn(
  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
  'hover:bg-gray-100 dark:hover:bg-gray-700',
  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-700',
)

/** Passive `·` divider between line-2 segments. */
export function Sep() {
  return <span aria-hidden="true" className="flex-shrink-0 text-gray-300 dark:text-gray-600">·</span>
}

interface CardMenuProps {
  /** Accessible name of the trigger, e.g. "More actions for session Alpha". */
  label: string
  /** Items, given the `close` every one of them must call before acting. */
  children: (close: () => void) => ReactNode
}

export function CardMenu({ label, children }: CardMenuProps) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const close = () => setOpen(false)

  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  useEffect(() => {
    if (!open) return
    pushOverlay()
    return () => popOverlay()
  }, [open])

  useEffect(() => {
    if (open) triggerRef.current?.focus()
  }, [open])

  return (
    <div
      className="relative flex-shrink-0"
      ref={menuRef}
      onKeyDown={(e) => {
        if (open && e.key === 'Escape') {
          e.stopPropagation()
          setOpen(false)
        }
      }}
    >
      <button
        type="button"
        ref={triggerRef}
        className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
          'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500',
          'active:bg-gray-200 dark:active:bg-gray-600')}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        title="More actions"
        onClick={() => setOpen((o) => !o)}
      >
        <MoreVertical size={16} aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className={cn(
            // z-20 keeps the open menu above the sticky group headers
            // (GroupHeader: `sticky top-0 z-10`); at an equal z-index a later
            // group's header would paint over a menu opened from a card above.
            'absolute top-full right-0 mt-1 min-w-[12rem] max-w-xs z-20',
            'rounded-md border border-gray-200 dark:border-gray-700',
            'bg-white dark:bg-gray-800 shadow-lg py-1',
          )}
        >
          {children(close)}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Run the SessionCard suites to record the baseline**

Run: `npm --prefix web test -- SessionCard`
Expected: PASS. Record the counts across all 12 files.

- [ ] **Step 3: Rewrite `SessionCard` onto `CardFrame` + `CardMenu`**

Delete from `SessionCard.tsx`: `actionBtn`, `menuItem`, `Sep`, the `articleRef`/`buttonRef`/`menuRef`/`menuTriggerRef` refs, `menuOpen` state, the four effects (scrollIntoView, listMode focus, outside-click, overlay, focus-trigger), the `isWide`/`swipe`/`PANEL_PX`/`restX` block, `handleCardClick`, the `<article>` + rail + panels + foreground JSX, the open `<button>`, and the ⋯ trigger + popup wrapper. Import them instead:

```tsx
import { CardFrame } from './card/CardFrame'
import { CardMenu, Sep, actionBtn, menuItem } from './card/CardMenu'
```

Keep `reduceMotion` (`useMediaQuery('(prefers-reduced-motion: reduce)')`) — the dot pulse and the autopilot marker read it, and a second call to the same media query is free.

`handleKeyDown` keeps the `matchShortcut(e, inGroup('list-mode'))` table but sheds its non-list-mode branch, which `CardFrame` now owns:

```tsx
  function handleKeyDown(e: React.KeyboardEvent) {
    // The registry's key→action table for a focused ROW, through the one
    // selection rule the shell's listener uses. No context is passed: a focused
    // row cannot honestly answer `hasOpenAction` or `sessionOpen`, so a
    // `list-mode` row that ever grows a `when` stops firing here — loudly, in
    // this component's tests — instead of being judged against a fabricated
    // context. CardFrame only calls this in list mode.
    const action = matchShortcut(e, inGroup('list-mode'))?.action
    if (!action) return
    e.preventDefault()
    switch (action.type) {
      case 'open': onOpen(session.id); break
      case 'continue': void handleSparkle(); break
      case 'rename': onRename(session.id); break
      case 'archive': onClose(session.id); break
      case 'permanent-delete': onDelete(session.id); break
      case 'navigate': onNavigate?.(action.dir); break
      case 'exit': onExit?.(); break
    }
  }
```

The `session.pending` early-return stays exactly as it is (its own `<article data-testid="session-card-pending">`).

The main return becomes:

```tsx
  return (
    <CardFrame
      id={session.id}
      openLabel={`Open session ${session.name}`}
      railClass={accent.rail}
      selected={selected}
      listMode={listMode}
      onOpen={onOpen}
      onKeyDown={handleKeyDown}
      swipeEnabled={!session.pending}
      swipeRight={{
        label: `Archive ${session.name}`,
        icon: <Archive size={20} aria-hidden="true" />,
        onCommit: () => onClose(session.id),
        testId: 'swipe-panel-archive',
        className: 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
      }}
      swipeLeft={{
        label: `Delete ${session.name}`,
        icon: <Trash2 size={20} aria-hidden="true" />,
        onCommit: () => onDelete(session.id),
        testId: 'swipe-panel-delete',
        className: 'bg-red-600 text-white',
      }}
      leading={hasSubAgents ? (/* …the existing chevron button, verbatim… */) : undefined}
      identity={<>{/* …the existing dot, autopilot marker and title spans, verbatim… */}</>}
      trailing={
        <>
          {/* …the existing sub-agent count, marker cluster, PrList, Fix PR,
              UsageBadge, model chip, account chip and ✦ resume button, all
              verbatim… */}
          <CardMenu label={`More actions for session ${session.name}`}>
            {(close) => (
              <>
                {/* …the existing suggestion / quick-reply / separator /
                    autopilot / Rename / Archive / Delete items, with each
                    `setMenuOpen(false)` replaced by `close()`… */}
              </>
            )}
          </CardMenu>
        </>
      }
    >
      {/* …the existing line-2 `card-context` div and the SubAgentRows, verbatim… */}
    </CardFrame>
  )
```

**Copy the JSX; do not retype it.** Every `data-testid`, `aria-label`, `title` and class string is asserted somewhere in the 12 suites. The only edits are `setMenuOpen(false)` → `close()`.

- [ ] **Step 4: Run the SessionCard suites and confirm the same counts**

Run: `npm --prefix web test -- SessionCard`
Expected: PASS with the Step 2 counts. A failure here is a real behaviour change — fix the extraction, never the assertion.

- [ ] **Step 5: Run the FULL suite and the gates, then commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/card/CardMenu.tsx web/src/components/SessionCard.tsx
git commit -F - <<'MSG'
refactor(web): SessionCard renders through CardFrame and CardMenu

No behaviour change — the twelve SessionCard suites pass unmodified. The frame
and the overflow menu are now reusable by a second card kind.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 8: `ArchivedSessionCard`

**Files:**
- Create: `web/src/components/ArchivedSessionCard.tsx`
- Test: `web/src/components/__tests__/ArchivedSessionCard.test.tsx`

**Interfaces:**
- Consumes: `CardFrame`, `CardMenu` + `Sep` + `menuItem` (Tasks 6–7), `relTime`/`isValidTimestamp` (`@/lib/relativeTime`), `useNow` (`@/hooks/useNow`), `formatRepoLabel` (`@/lib/repoLabel`), `repoFromCwd` (`@/lib/repoFromCwd`).
- Produces:

```ts
export function ArchivedSessionCard(props: {
  session: ArchivedSession
  selected?: boolean
  listMode?: boolean
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  onDelete: (session: ArchivedSession) => void
  unarchiving?: boolean
}): JSX.Element
export function displayNameOf(s: ArchivedSession): string
```

`displayNameOf` moves here from `ArchivedManager` — the card is what renders the name, and Task 9's list imports it for the delete dialog's `names`.

**What the card does NOT show.** No status pill, model chip, host, PR list, sub-agents, quick replies or autopilot marker. `ArchivedSession` carries none of those fields (D5); inventing a placeholder for them would be a lie about a dead session.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/ArchivedSessionCard.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ArchivedSessionCard } from '../ArchivedSessionCard'
import { archivedSession } from '@/test/factories'

function props(over = {}) {
  return {
    session: archivedSession({ id: 'a1', name: 'Old project', account: 'alice', repo: 'cairnea/KinDash' }),
    onReview: vi.fn(),
    onUnarchive: vi.fn(),
    onDelete: vi.fn(),
    ...over,
  }
}

describe('ArchivedSessionCard', () => {
  it('shows the name, the repo and the account', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.getByText('Old project')).toBeInTheDocument()
    expect(screen.getByText('cairnea/KinDash')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
  })

  it('falls back to a short id when the session has no name', () => {
    render(<ArchivedSessionCard {...props({ session: archivedSession({ id: 'abcdef123456', name: '' }) })} />)
    expect(screen.getByText('abcdef12')).toBeInTheDocument()
  })

  it('labels the archive time relatively, not as a raw locale string', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.getByTestId('archived-at').textContent).toMatch(/^Archived \d+[smhd] ago$/)
  })

  it('opens the read-only transcript from the card', () => {
    const onReview = vi.fn()
    render(<ArchivedSessionCard {...props({ onReview })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Review Old project' }))
    expect(onReview).toHaveBeenCalledWith('a1')
  })

  it('offers Review, Unarchive and Delete permanently in the overflow menu', async () => {
    const onUnarchive = vi.fn()
    render(<ArchivedSessionCard {...props({ onUnarchive })} />)
    await userEvent.click(screen.getByRole('button', { name: 'More actions for Old project' }))
    expect(screen.getByRole('menuitem', { name: 'Review' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Delete permanently' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('menuitem', { name: 'Unarchive' }))
    expect(onUnarchive).toHaveBeenCalledWith('a1')
  })

  it('commits Unarchive on a right-swipe and Delete on a left-swipe', () => {
    const onUnarchive = vi.fn()
    const onDelete = vi.fn()
    const p = props({ onUnarchive, onDelete })
    render(<ArchivedSessionCard {...p} />)
    const el = screen.getByTestId('card-swipe-fg')
    Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 })

    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 })
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onUnarchive).toHaveBeenCalledWith('a1')

    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 100, clientY: 45 })
    fireEvent.pointerUp(window, { clientX: 100, clientY: 45 })
    expect(onDelete).toHaveBeenCalledWith(p.session)
  })

  it('carries none of the live-session signals', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.queryByTestId('card-status-dot')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-prs')).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- ArchivedSessionCard`
Expected: FAIL — module unresolved.

- [ ] **Step 3: Write the card**

```tsx
// web/src/components/ArchivedSessionCard.tsx
//
// One archived session, as a card.
//
// This is the whole of what archived rows lost by having their own renderer: a
// rail, swipe actions, a keyboard-reachable open control, an overflow menu, and
// a relative timestamp that matches every other time in the app. The rail is
// deliberately MUTED rather than a status colour — an archived session has no
// status, and borrowing one would put a live-looking signal on a dead row.
//
// It shows nothing a live card shows that ArchivedSession has no field for: no
// status pill, model chip, host, PRs, sub-agents, quick replies or autopilot
// marker. See the design spec's D5.
import { Archive, ArchiveRestore, FolderGit2, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { repoFromCwd } from '@/lib/repoFromCwd'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { useNow } from '@/hooks/useNow'
import type { ArchivedSession } from '@/api/types'
import { CardFrame } from './card/CardFrame'
import { CardMenu, Sep, menuItem } from './card/CardMenu'

/** An archived row's display name: its name, or a short id for the unnamed. */
export function displayNameOf(s: ArchivedSession): string {
  return s.name || s.id.slice(0, 8)
}

interface ArchivedSessionCardProps {
  session: ArchivedSession
  selected?: boolean
  listMode?: boolean
  /** Open the read-only transcript. */
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  /** Destructive: the caller confirms it before mutating. */
  onDelete: (session: ArchivedSession) => void
  unarchiving?: boolean
}

export function ArchivedSessionCard({
  session, selected = false, listMode = false, onReview, onUnarchive, onDelete, unarchiving = false,
}: ArchivedSessionCardProps) {
  const name = displayNameOf(session)
  // Prefer the accurate repo field (GitHub nameWithOwner); fall back to the
  // cwd-derived name only for legacy/scratch rows that never recorded one.
  const repoLabel = formatRepoLabel(session.repo ?? repoFromCwd(session.cwd))
  const now = useNow()

  return (
    <CardFrame
      id={session.id}
      openLabel={`Review ${name}`}
      // Muted: an archived session has no status tier to colour.
      railClass="bg-neutral/40"
      selected={selected}
      listMode={listMode}
      onOpen={onReview}
      swipeRight={{
        label: `Unarchive ${name}`,
        icon: <ArchiveRestore size={20} aria-hidden="true" />,
        onCommit: () => onUnarchive(session.id),
        testId: 'swipe-panel-unarchive',
        className: 'bg-blue-600 text-white',
      }}
      swipeLeft={{
        label: `Delete ${name}`,
        icon: <Trash2 size={20} aria-hidden="true" />,
        onCommit: () => onDelete(session),
        testId: 'swipe-panel-delete',
        className: 'bg-red-600 text-white',
      }}
      identity={
        <span className="min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100">
          {name}
        </span>
      }
      trailing={
        <>
          <span
            className="flex-shrink-0 max-w-[30%] truncate text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded"
            title={session.account}
          >
            {session.account}
          </span>
          <CardMenu label={`More actions for ${name}`}>
            {(close) => (
              <>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onReview(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Archive size={16} aria-hidden="true" />
                  Review
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={unarchiving}
                  onClick={() => { close(); onUnarchive(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200 disabled:opacity-40')}
                >
                  <ArchiveRestore size={16} aria-hidden="true" />
                  {unarchiving ? 'Restoring…' : 'Unarchive'}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onDelete(session) }}
                  className={cn(menuItem, 'text-red-600 dark:text-red-400')}
                >
                  <Trash2 size={16} aria-hidden="true" />
                  Delete permanently
                </button>
              </>
            )}
          </CardMenu>
        </>
      }
    >
      <div data-testid="card-context" className="mt-0.5 flex items-center gap-1.5 min-w-0 text-xs">
        <span
          className={cn(
            'min-w-0 inline-flex items-center gap-1',
            repoLabel.isScratch
              ? 'text-gray-400 dark:text-gray-500 italic'
              : 'text-gray-600 dark:text-gray-300',
          )}
          title={repoLabel.text}
        >
          <FolderGit2 size={12} aria-hidden="true" className="flex-shrink-0" />
          <span className="truncate">{repoLabel.text}</span>
        </span>
        {isValidTimestamp(session.archivedAt) && (
          <>
            <Sep />
            <span
              data-testid="archived-at"
              className="ml-auto flex-shrink-0 text-gray-400 dark:text-gray-500 tabular-nums"
              title={`Archived ${new Date(session.archivedAt).toLocaleString()}`}
            >
              Archived {relTime(session.archivedAt, now || undefined)}
            </span>
          </>
        )}
      </div>
    </CardFrame>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix web test -- ArchivedSessionCard`
Expected: PASS — 7 tests.

- [ ] **Step 5: Run the gates and commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/components/ArchivedSessionCard.tsx web/src/components/__tests__/ArchivedSessionCard.test.tsx
git commit -F - <<'MSG'
feat(web): ArchivedSessionCard — archived rows as session cards

Muted rail, relative "Archived 3d ago", swipe to unarchive or delete, and an
overflow menu, all through the shared card frame.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 9: `ArchivedSessionList` replaces `ArchivedManager`

**Files:**
- Create: `web/src/components/ArchivedSessionList.tsx`
- Delete: `web/src/components/ArchivedManager.tsx`
- Modify: `web/src/shell/routes.tsx:153` (the mount), `web/src/components/__tests__/dedupeMobileDesktop.test.ts:60-100` (the guard)
- Rename + update: `web/src/components/__tests__/ArchivedTab.test.tsx` → `ArchivedSessionList.actions.test.tsx`, `ArchivedManager.test.tsx` → `ArchivedSessionList.test.tsx`
- Modify: `web/src/shell/routes.test.tsx:64` (the page stub's module path)

**Interfaces:**
- Consumes: `ArchivedSessionCard` + `displayNameOf` (Task 8), the shared list chrome (PR 1), `useArchivedSessions`/`useUnarchiveSession`/`usePermanentDeleteSession`/`useBulkUnarchiveSessions`/`useBulkPermanentDeleteSessions`, `useSessionSearch('archived')`, `useItemOrdering(…, ARCHIVED_SPEC, …)`.
- Produces: `<ArchivedSessionList onReviewSession={(id: string) => void} />` — the same single prop `ArchivedManager` had, so `routes.tsx` changes only the component name and the import path.

- [ ] **Step 1: Create the new module from `ArchivedManager`'s body**

`git mv web/src/components/ArchivedManager.tsx web/src/components/ArchivedSessionList.tsx`, rename the exported function, and make these changes and no others:

- Drop `displayNameOf` (import it from `./ArchivedSessionCard`).
- Drop the row's date formatting and `renderArchivedRow`'s markup entirely; replace with:

```tsx
  function renderArchivedRow(s: ArchivedSession) {
    const card = (
      <ArchivedSessionCard
        session={s}
        onReview={onReviewSession}
        onUnarchive={handleUnarchive}
        onDelete={handlePermanentDelete}
        unarchiving={unarchivingId === s.id}
      />
    )
    return (
      <SelectableRow
        key={s.id}
        id={s.id}
        name={displayNameOf(s)}
        checked={selection.isSelected(s.id)}
        onToggle={selection.toggleOne}
      >
        {card}
      </SelectableRow>
    )
  }
```

- Update the module docstring to name the new component and to record that the row markup now lives in `ArchivedSessionCard`.

Everything else — `QuickPurgeBar`, `summarize`, the mutations and their handlers, the select-all header, the controls strip, the two dialogs, the `hasFilteredSessions` collapse guard — moves unchanged.

- [ ] **Step 2: Update the mount and the guard**

In `web/src/shell/routes.tsx`, change the import to `import { ArchivedSessionList } from '@/components/ArchivedSessionList'` and the JSX to `<ArchivedSessionList onReviewSession={(id) => navigate(archivedSessionPath(id))} />`.

In `web/src/shell/routes.test.tsx:64`, change the stub to
`vi.mock('@/components/ArchivedSessionList', () => ({ ArchivedSessionList: stub('archived') }))`.

In `web/src/components/__tests__/dedupeMobileDesktop.test.ts`, update the `#43` block: replace `<ArchivedManager\b` with `<ArchivedSessionList\b` in both the positive and the per-shell negative assertions, and extend the re-inline guard's alternation to `(AccountRow|AccountsStrip|DeleteAccountDialog|ArchivedList|ArchivedSessionCard)`.

- [ ] **Step 3: Rename and update the two archived suites**

`git mv` both files as listed. In each, change the import to `ArchivedSessionList` and update **only** the assertions that named the deleted row markup:

- The "rows render with name/account/date" case now asserts the relative label: `expect(screen.getByTestId('archived-at').textContent).toMatch(/Archived .* ago/)` instead of the `toLocaleString` output. The `ArchivedManager.test.tsx` case that pins "date **and time**" formatting is superseded — replace it with the relative assertion and note in the test's comment that the format moved to `lib/relativeTime` (one owner for every time in the app).
- Review / Unarchive / Delete are now reached through the ⋯ menu or the card, so those cases open the menu first (`await userEvent.click(screen.getByRole('button', { name: /More actions for/ }))`) before clicking the item.

Every other assertion — empty state, dialog confirm/cancel/Escape, bulk bar, select-all, shift-range, search no-match + Clear, group-by Account, collapse recoverability, bulk-delete progress + cancel, all four quick-purge thresholds and "purges exactly the oldest N" — must pass with no edit.

- [ ] **Step 4: Run the archived suites**

Run: `npm --prefix web test -- ArchivedSessionList dedupeMobileDesktop routes`
Expected: PASS.

- [ ] **Step 5: Run the FULL suite and the gates, then commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add -A web/src/components web/src/shell/routes.tsx web/src/shell/routes.test.tsx
git commit -F - <<'MSG'
refactor(web): ArchivedSessionList replaces ArchivedManager

The archived list now renders ArchivedSessionCard through the shared list
chrome. ArchivedManager and its bespoke row markup are deleted; the
dedupeMobileDesktop guard follows the rename and gains the new card.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 10: Serve the real `archived_at`

**Files:**
- Modify: `server/sessions/orchestrator.py:10246` (inside `list_archived`), `server/db/models.py:210-215` (the `updated_at` docstring)
- Test: `server/tests/test_archived_at.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no signature change. `GET /api/sessions/archived` keeps its exact wire shape; only the value of `archivedAt` changes.

`list_archived` serves `archivedAt` from `row.updated_at`, which is bumped by **every** row write — a status change, a rename, a sub-agent update. The card's "Archived 3d ago" and the archived-date sort both read it, so an incidental write silently makes an old archive look recent. The real column exists (`archived_at`, set in `archive_session`); rows archived before it was added have `NULL`, which is the only reason the fallback exists.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_archived_at.py`:

```python
# ---------------------------------------------------------------------------
# list_archived serves archived_at, not updated_at
# ---------------------------------------------------------------------------


def test_list_archived_serves_archived_at_not_updated_at(tmp_path):
    """A row write AFTER archiving must not move the reported archivedAt.

    `updated_at` bumps on every write (rename, status, sub-agents). Serving it
    as "archived at" made an old archive look freshly archived, and reordered
    the archived list's default sort behind any incidental touch.
    """
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'archived_at_list.db'}"

    async def run():
        await init_db(db_url=db_url)
        archived_at = datetime(2026, 1, 1, 12, 0, 0)
        async with db_session() as db:
            db.add(DbSession(
                id="a1", name="Old", account_name="acc", claude_session_id=None,
                status="archived", cwd="/tmp/a1", archived_at=archived_at,
            ))
        # A later, unrelated write — exactly what bumps updated_at in the wild.
        async with db_session() as db:
            row = await db.get(DbSession, "a1")
            row.name = "Old (renamed)"

        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        rows = await orch.list_archived()

        async with db_session() as db:
            fresh = await db.get(DbSession, "a1")
            return rows, fresh.updated_at

    rows, updated_at = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0]["archivedAt"].startswith("2026-01-01T12:00:00"), rows[0]["archivedAt"]
    assert not rows[0]["archivedAt"].startswith(updated_at.isoformat()[:19]), (
        "archivedAt still tracks updated_at"
    )


def test_list_archived_falls_back_to_updated_at_for_legacy_rows(tmp_path):
    """A row archived before the archived_at column existed has NULL there.

    It must still report a time rather than null — the card's label and the
    default sort both read this field.
    """
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'archived_at_legacy.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id="legacy", name="Ancient", account_name="acc", claude_session_id=None,
                status="archived", cwd="/tmp/legacy", archived_at=None,
            ))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        return await orch.list_archived()

    rows = asyncio.run(run())
    assert rows[0]["archivedAt"] is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_archived_at.py -k list_archived -v`
Expected: FAIL — `test_list_archived_serves_archived_at_not_updated_at` reports the renamed row's `updated_at`.

- [ ] **Step 3: Make the change**

In `server/sessions/orchestrator.py`, inside `list_archived`'s dict comprehension:

```python
                "createdAt": iso_utc(row.created_at),
                # `archived_at` is the lifecycle fact; `updated_at` bumps on every
                # incidental row write and only stands in for rows archived before
                # that column existed (it is NULL on those).
                "archivedAt": iso_utc(row.archived_at or row.updated_at),
```

In `server/db/models.py`, amend the `updated_at` comment: replace "Used for the archived-sessions list's 'archived at' only" with "Used as the archived-sessions list's 'archived at' only as a legacy fallback, when `archived_at` is NULL".

- [ ] **Step 4: Run the test to verify it passes**

Run: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_archived_at.py server/tests/test_archive.py -v`
Expected: PASS.

- [ ] **Step 5: Run the four-step gate and commit**

```bash
/home/tony/GitHub/ccswitch/.venv/bin/python -m ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ 2>&1 | tee /tmp/ccswitch-core-tests.log
/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ 2>&1 | tee /tmp/ccswitch-server-tests.log
git add server/sessions/orchestrator.py server/db/models.py server/tests/test_archived_at.py
git commit -F - <<'MSG'
fix(archive): serve the real archived_at, not the last row write

list_archived reported updated_at, which bumps on every write — so renaming an
archived session made it look freshly archived and jumped it to the top of the
default sort. archived_at is the lifecycle fact; updated_at now only stands in
for legacy rows where it is NULL.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

**PR 2 ends here.** Visible change: archived rows look and behave like session cards, and their timestamps are honest.

---

# PR 3 — The scope switch

Deliverable: Archived leaves the nav and becomes a scope inside the Sessions list.

---

### Task 11: Routes move under `/sessions`

**Files:**
- Modify: `web/src/session/sessionNavState.ts:39-67`, `web/src/shell/routes.tsx`
- Test: `web/src/session/__tests__/sessionNavState.test.ts` (create), `web/src/shell/routes.test.tsx`

**Interfaces:**
- Consumes: `SESSIONS_PATH`.
- Produces:

```ts
export const ARCHIVED_PATH: string              // '/sessions/archived'
export const ARCHIVED_SESSION_ROUTE: string     // '/sessions/archived/:archivedId'
export const LEGACY_ARCHIVED_PATH = '/archived'
export const LEGACY_ARCHIVED_SESSION_ROUTE = '/archived/:sessionId'
export function archivedSessionPath(id: string): string
export function listScopeForPath(pathname: string): 'live' | 'archived'
```

`ARCHIVED_PATH` and `archivedSessionPath` keep their names and change their values, so `useArchiveAdvance`, `routes.tsx`'s `AutomationsPage` and `ReviewPage`, and `ReviewView`'s back button need no edit at all.

**The param rename is load-bearing.** `/sessions/archived/:archivedId` must NOT use `:id`: `useOpenSessionId()` reads `useParams().id`, and reusing the name would attach a live `SessionRuntimeProvider` to an archived transcript. (D2)

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/session/__tests__/sessionNavState.test.ts
import { describe, it, expect } from 'vitest'
import {
  ARCHIVED_PATH, ARCHIVED_SESSION_ROUTE, SESSIONS_PATH,
  archivedSessionPath, listScopeForPath,
} from '../sessionNavState'

describe('archived paths', () => {
  it('nests the archived scope under the sessions section', () => {
    expect(ARCHIVED_PATH).toBe(`${SESSIONS_PATH}/archived`)
    expect(archivedSessionPath('abc')).toBe('/sessions/archived/abc')
  })

  it('names the archived detail param `archivedId`, never `id`', () => {
    // `useOpenSessionId` reads useParams().id. Reusing the name here would
    // attach a live session runtime to a read-only transcript.
    expect(ARCHIVED_SESSION_ROUTE).toBe('/sessions/archived/:archivedId')
    expect(ARCHIVED_SESSION_ROUTE).not.toContain(':id')
  })
})

describe('listScopeForPath', () => {
  it.each([
    ['/sessions', 'live'],
    ['/sessions/abc', 'live'],
    ['/sessions/abc/agents/x', 'live'],
    ['/sessions/archived', 'archived'],
    ['/sessions/archived/abc', 'archived'],
    ['/pipeline', 'live'],
  ])('%s → %s', (path, scope) => {
    expect(listScopeForPath(path)).toBe(scope)
  })

  it('does not mistake a session whose id merely starts with "archived"', () => {
    expect(listScopeForPath('/sessions/archivedxyz')).toBe('live')
  })
})
```

Add to `web/src/shell/routes.test.tsx` — in the redirects table:

```ts
    ['/archived', '/sessions/archived'],
    ['/archived/abc', '/sessions/archived/abc'],
    ['/review/abc', '/sessions/archived/abc'],
```

…replacing the existing `['/review/abc', '/archived/abc']` row. In the pages table, replace the two `/archived*` rows with:

```ts
    ['/sessions/archived', 'page-archived'],
    ['/sessions/archived/abc', 'page-review'],
```

And add:

```tsx
  it('keeps the kiosk params across the legacy archived redirect', () => {
    renderAt('/archived?kiosk=1')
    expect(url()).toBe('/sessions/archived?kiosk=1')
  })

  it('ranks the static archived route above the session detail route', () => {
    // `/sessions/archived` and `/sessions/:id` are siblings. If the dynamic one
    // won, the archived list would render as a session whose id is "archived".
    renderAt('/sessions/archived')
    expect(screen.getByTestId('page-archived')).toBeInTheDocument()
    expect(screen.queryByTestId('page-session-detail')).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm --prefix web test -- sessionNavState routes`
Expected: FAIL — `listScopeForPath` unresolved; the redirect and page rows land on the old paths.

- [ ] **Step 3: Update `sessionNavState.ts`**

```ts
/** `/sessions` — the session list. */
export const SESSIONS_PATH = NAV_BY_ID.sessions.path

/**
 * `/sessions/archived` — the archived SCOPE of the sessions list.
 *
 * Archived was its own nav section until the scope switch landed. It is a scope
 * now, so its paths hang off `SESSIONS_PATH`: `navDefForPath` resolves them to
 * `sessions`, which is what keeps the list column beside the read-only
 * transcript on desktop. The constant keeps its name — every caller
 * (`useArchiveAdvance`, the automations page, `ReviewView`'s back button) was
 * already routing through it.
 */
export const ARCHIVED_PATH = `${SESSIONS_PATH}/archived`

export const SESSION_ROUTE = `${SESSIONS_PATH}/:id`
export const SUBAGENT_ROUTE = `${SESSIONS_PATH}/:id/agents/:agentId`

/**
 * The archived transcript route. Its param is `archivedId`, NOT `id`, and that
 * is the entire mechanism keeping a live runtime off a dead session:
 * `useOpenSessionId` reads `useParams().id`, so an archived transcript simply
 * has no session id to find. Rename this param and `PageHost` will mount a
 * `SessionRuntimeProvider` over an archived transcript with nothing to fail.
 */
export const ARCHIVED_SESSION_ROUTE = `${ARCHIVED_PATH}/:archivedId`

/** Pre-scope-switch spellings. Bookmarks only — nothing in the app builds these. */
export const LEGACY_ARCHIVED_PATH = '/archived'
export const LEGACY_ARCHIVED_SESSION_ROUTE = '/archived/:sessionId'

export function sessionPath(id: string): string {
  return `${SESSIONS_PATH}/${id}`
}

export function subAgentPath(id: string, agentId: string): string {
  return `${SESSIONS_PATH}/${id}/agents/${agentId}`
}

/** The route of an archived session's read-only transcript. */
export function archivedSessionPath(id: string): string {
  return `${ARCHIVED_PATH}/${id}`
}

/**
 * Which scope of the sessions list a route is in.
 *
 * Scope is derived from the URL and nowhere else — not a `?scope=` param (this
 * shell has a history of overlay params leaking onto unrelated URLs) and not
 * localStorage (a bookmark or the Back button must land on the scope it names).
 */
export function listScopeForPath(pathname: string): 'live' | 'archived' {
  return pathname === ARCHIVED_PATH || pathname.startsWith(`${ARCHIVED_PATH}/`)
    ? 'archived'
    : 'live'
}
```

- [ ] **Step 4: Update `routes.tsx`**

Import `LEGACY_ARCHIVED_PATH`, `LEGACY_ARCHIVED_SESSION_ROUTE` and `useLocation`. `ReviewPage` reads the new param:

```tsx
/** `/sessions/archived/:archivedId` — read-only transcript. Never gets a live
 *  runtime: the param is not `id`, so `useOpenSessionId` finds nothing. */
function ReviewPage() {
  const { archivedId } = useParams()
  const navigate = useNavigate()
  if (!archivedId) return <Navigate to={ARCHIVED_PATH} replace />
  return <ReviewView sessionId={archivedId} onBack={() => navigate(ARCHIVED_PATH)} />
}
```

Add the two redirects, beside the existing ones:

```tsx
/** `/archived` → `/sessions/archived`. The search is carried so a reload of the
 *  redirected URL is still `?kiosk=1`. */
function RedirectLegacyArchived() {
  const { search } = useLocation()
  return <Navigate to={{ pathname: ARCHIVED_PATH, search }} replace />
}

function RedirectLegacyArchivedSession() {
  const { sessionId } = useParams()
  const { search } = useLocation()
  return (
    <Navigate
      to={{ pathname: sessionId ? archivedSessionPath(sessionId) : ARCHIVED_PATH, search }}
      replace
    />
  )
}
```

In the table, declare the archived routes **before** `SESSION_ROUTE` (react-router ranks a static segment above a dynamic one regardless, but the order documents the dependency), and move the legacy paths into the redirect block:

```tsx
  { path: SESSIONS_PATH, element: <SessionsPage /> },
  // Before SESSION_ROUTE: `/sessions/archived` is a static sibling of
  // `/sessions/:id`, and the reader must not resolve as a session id.
  { path: ARCHIVED_PATH, element: <ArchivedPage /> },
  { path: ARCHIVED_SESSION_ROUTE, element: <ReviewPage /> },
  { path: SESSION_ROUTE, element: <SessionsPage /> },
  { path: SUBAGENT_ROUTE, element: <SubAgentPage /> },
  …
  // Legacy paths — bookmarks only.
  { path: LEGACY_ARCHIVED_PATH, element: <RedirectLegacyArchived /> },
  { path: LEGACY_ARCHIVED_SESSION_ROUTE, element: <RedirectLegacyArchivedSession /> },
```

`RedirectReview` (`/review/:sessionId`) needs no change — it already builds through `archivedSessionPath`, so it now lands on the new spelling directly.

- [ ] **Step 5: Update the other suites that name the old paths**

- `web/src/shell/regions/PageHost.test.tsx:175` — `PROBE_PAGES` key `'/archived/:id'` → `'/sessions/archived/:archivedId'`; update the two comments (lines 167–171, 338–343) to say the sections are the same but the PARAM NAME differs, which is now what makes the gate load-bearing.
- `web/src/App.test.tsx:151,173` — expect `/sessions/archived/sess-1` and `/sessions/archived/arch-1`.
- `web/src/App.dialogs.test.tsx:92` — `/archived?dialog=new` → `/sessions/archived?dialog=new`.
- `web/src/shell/regions/DrawerHost.test.tsx:292-294` — `/archived?drawer=help` → `/sessions/archived?drawer=help`.
- `web/src/features/sessions/useArchiveAdvance.test.tsx:144` — expect `/sessions/archived/gone`.

- [ ] **Step 6: Run the affected suites, then the gates, then commit**

Run: `npm --prefix web test -- sessionNavState routes PageHost App DrawerHost useArchiveAdvance`
Expected: PASS.

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/session/ web/src/shell/ web/src/App.test.tsx web/src/App.dialogs.test.tsx web/src/features/sessions/useArchiveAdvance.test.tsx
git commit -F - <<'MSG'
refactor(web): archived routes move under /sessions/archived

The reader's param becomes `archivedId`, which is what keeps a live runtime off
it now that it shares a section with the live list. /archived and /archived/:id
redirect, carrying their search so a kiosk URL survives.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 12: `useOpenItem`

**Files:**
- Modify: `web/src/shell/openSessionId.ts`, `web/src/shell/AppShell.tsx:212-222`
- Test: `web/src/shell/openSessionId.test.tsx` (create)

**Interfaces:**
- Consumes: `navDefForPath`.
- Produces:

```ts
export type OpenItem = { kind: 'session'; id: string } | { kind: 'archived'; id: string }
export function useOpenItem(): OpenItem | null
export function useOpenSessionId(): string | null   // unchanged signature
```

**Why this exists.** `AppShellInner` derives `itemOpen` from a bare `useParams().id`. With the reader now under `/sessions`, `id` is undefined there — so on a phone the list would stay up on top of the transcript, and `app.list`'s visibility rule (`c.mode === 'desktop' || !c.itemOpen`) would be answering a different question from `PageHost`'s runtime gate. One derivation, two consumers. (D3)

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/shell/openSessionId.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_SESSION_ROUTE, ARCHIVED_PATH, SESSIONS_PATH } from '@/session/sessionNavState'
import { useOpenItem, useOpenSessionId } from './openSessionId'

function Probe() {
  const item = useOpenItem()
  const sessionId = useOpenSessionId()
  return <div data-testid="probe">{`${item ? `${item.kind}:${item.id}` : 'none'}|${sessionId ?? '-'}`}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[SESSIONS_PATH, SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_PATH, ARCHIVED_SESSION_ROUTE, '/pipeline'].map((p) => (
          <Route key={p} path={p} element={<Probe />} />
        ))}
      </Routes>
    </MemoryRouter>,
  )
}

describe('useOpenItem', () => {
  it.each([
    ['/sessions', 'none|-'],
    ['/sessions/abc', 'session:abc|abc'],
    ['/sessions/abc/agents/x', 'session:abc|abc'],
    ['/sessions/archived', 'none|-'],
    ['/pipeline', 'none|-'],
  ])('%s → %s', (path, expected) => {
    renderAt(path)
    expect(screen.getByTestId('probe')).toHaveTextContent(expected)
  })

  it('reports an archived transcript as open, but never as an open SESSION', () => {
    // Both halves matter: the shell must know something is open (so a phone
    // shows the reader instead of the list), and PageHost must not attach a
    // live runtime to it.
    renderAt('/sessions/archived/abc')
    expect(screen.getByTestId('probe')).toHaveTextContent('archived:abc|-')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web test -- openSessionId`
Expected: FAIL — `useOpenItem` is not exported.

- [ ] **Step 3: Rewrite `openSessionId.ts`**

Keep the existing module docstring and extend it; replace the hook:

```ts
import { useLocation, useParams } from 'react-router-dom'
import { navDefForPath } from './navDefs'

/** What the current route has open, if anything. */
export type OpenItem =
  | { kind: 'session'; id: string }
  | { kind: 'archived'; id: string }

/**
 * The open item, or null.
 *
 * Three consumers must agree on this and they ask for different halves of it:
 *
 *   · `PageHost` mounts `SessionRuntimeProvider` only for `kind: 'session'`.
 *   · `PromptDock` branches on the same answer — render `SessionComposer`
 *     outside that provider and `useSessionRuntimeContext()` throws, taking the
 *     whole route down.
 *   · `AppShell` needs "is SOMETHING open" for the region matrix, because a
 *     phone gives its one content band to either the list or the page.
 *
 * The archived reader is the case that splits them: it IS open (the phone must
 * show it, not the list) and it is NOT a session (no runtime). It is
 * distinguished by its param NAME — `/sessions/archived/:archivedId` — which is
 * why that name is load-bearing rather than cosmetic.
 */
export function useOpenItem(): OpenItem | null {
  const { id, archivedId } = useParams()
  const { pathname } = useLocation()
  if (archivedId) return { kind: 'archived', id: archivedId }
  if (id && navDefForPath(pathname)?.id === 'sessions') return { kind: 'session', id }
  return null
}

/** The open LIVE session's id, or null. */
export function useOpenSessionId(): string | null {
  const item = useOpenItem()
  return item?.kind === 'session' ? item.id : null
}
```

- [ ] **Step 4: Wire `AppShell`**

In `AppShellInner`, replace `const { id } = useParams()` with `const openItem = useOpenItem()` (importing it beside the existing `useOpenSessionId` import), drop the now-unused `useParams` import if nothing else uses it, and change the region context:

```tsx
  const region: RegionContext = {
    mode,
    hasList: def?.hasList ?? false,
    // "Is something open", not "is there an `:id`" — the archived reader has no
    // `:id` and must still take the phone's single content band.
    itemOpen: openItem !== null,
    inSessions: def?.id === 'sessions',
    drawer,
  }
```

- [ ] **Step 5: Add the phone-layout assertion**

This goes in `web/src/shell/AppShell.test.tsx`, **not** `visibilityMatrix.test.tsx`. That file tests the visibility RULES as a pure predicate over a hand-built `RegionContext`; the claim here is that the shell *derives* the right context from a route, which is what `AppShell.test.tsx` is for (its own header says so). Use its existing `renderShell({ path, mode })` harness and `presentAppRegions()` from `@/test/regions`:

```tsx
  it('hides the list behind the archived reader on a phone', async () => {
    // A phone gives its one content band to EITHER the list or the page, and
    // the reader is a page. Before `useOpenItem` the shell asked whether there
    // was an `:id`; the reader has `:archivedId`, so the list stayed up on top
    // of the transcript.
    renderShell({ path: '/sessions/archived/abc', mode: 'phone' })
    await waitFor(() => expect(presentAppRegions()).not.toContain('app.list'))
  })

  it('keeps the list beside the archived reader on desktop', async () => {
    renderShell({ path: '/sessions/archived/abc', mode: 'desktop' })
    await waitFor(() => expect(presentAppRegions()).toContain('app.list'))
  })
```

Read the file's neighbouring region assertions first and match how they await the shell's first paint — copy that shape rather than the `waitFor` sketched here if it differs.

- [ ] **Step 6: Run the affected suites, the gates, then commit**

Run: `npm --prefix web test -- openSessionId AppShell visibilityMatrix PageHost PromptDock`
Expected: PASS.

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/shell/
git commit -F - <<'MSG'
refactor(shell): one derivation of "what is open" for the runtime gate and the layout

The archived reader is open but is not a session. AppShell asked for an `:id`,
which the reader does not have, so a phone would have kept the list on top of
the transcript.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 13: `ScopeSegment`, `useArchivedListState`, and the column switch

**Files:**
- Create: `web/src/features/sessions/ScopeSegment.tsx`, `web/src/features/sessions/useArchivedListState.ts`
- Modify: `web/src/features/sessions/SessionListColumn.tsx`, `web/src/shell/AppShell.tsx` (pass `scope`)
- Test: `web/src/features/sessions/ScopeSegment.test.tsx` (create), `web/src/features/sessions/SessionListColumn.test.tsx` (extend)

**Interfaces:**
- Consumes: `listScopeForPath` (Task 11), `useArchivedSessions`, `useSessionSearch`, `useItemOrdering` + `ARCHIVED_SPEC`, `ArchivedSessionList` (Task 9).
- Produces:
  - `<ScopeSegment scope={'live'|'archived'} activeCount={number} archivedCount={number} />`
  - `useArchivedListState(): { sessions: readonly ArchivedSession[]; search: SessionSearch; ordering: OrderingHandle<ArchGroupBy, ArchSortBy, ArchivedSession> }`
  - `SessionListColumnProps` gains `scope: 'live' | 'archived'` and `onReviewSession: (id: string) => void`.

**Tokens, not palette.** Both new files are under `src/features/`, where `ccswitch/no-raw-palette` is enforced. `ScopeSegment` uses `border-edge`, `text-ink`, `text-ink-muted`, `bg-accent-wash`, `text-accent`, `min-h-target`, `focus-visible:ring-accent`.

**No kiosk gate is needed.** `app.list` is already absent in kiosk (`REGION_VISIBILITY['app.list']` starts with `c.mode !== 'kiosk'`), and `/sessions/archived` now resolves to the `sessions` nav def, whose `showInKiosk: false` makes `KioskPageGuard` redirect it. Both protections that the deleted nav entry provided survive without a new flag.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/features/sessions/ScopeSegment.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { ScopeSegment } from './ScopeSegment'

function Url() {
  const { pathname } = useLocation()
  return <div data-testid="url">{pathname}</div>
}

function renderSegment(scope: 'live' | 'archived', at = '/sessions') {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <ScopeSegment scope={scope} activeCount={7} archivedCount={132} />
      <Url />
    </MemoryRouter>,
  )
}

describe('ScopeSegment', () => {
  it('is a named navigation carrying both counts', () => {
    renderSegment('live')
    expect(screen.getByRole('navigation', { name: 'Session scope' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Active/ })).toHaveTextContent('7')
    expect(screen.getByRole('link', { name: /Archived/ })).toHaveTextContent('132')
  })

  it('marks the current scope with aria-current', () => {
    renderSegment('archived', '/sessions/archived')
    expect(screen.getByRole('link', { name: /Archived/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /Active/ })).not.toHaveAttribute('aria-current')
  })

  it('navigates to the archived scope', async () => {
    renderSegment('live')
    await userEvent.click(screen.getByRole('link', { name: /Archived/ }))
    expect(screen.getByTestId('url')).toHaveTextContent('/sessions/archived')
  })

  it('omits a zero count rather than showing "0"', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <ScopeSegment scope="live" activeCount={0} archivedCount={0} />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: /Active/ }).textContent).toBe('Active')
  })
})
```

Extend `web/src/features/sessions/SessionListColumn.test.tsx` with a new describe block (using its existing `renderColumn` harness, extended to pass `scope`):

```tsx
describe('SessionListColumn — scope switch', () => {
  it('renders the live sections and the New Session button in the live scope', () => {
    renderColumn({ sessions: [session({ id: 'sess-1', name: 'First' })], scope: 'live' })
    expect(screen.getByRole('button', { name: 'New Session' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Session scope' })).toBeInTheDocument()
  })

  it('drops New Session and the live sections in the archived scope', () => {
    renderColumn({
      sessions: [session({ id: 'sess-2', name: 'Parked', status: 'pending_approval' })],
      scope: 'archived',
    })
    expect(screen.queryByRole('button', { name: 'New Session' })).not.toBeInTheDocument()
    // PendingApprovalSection belongs to the live list only.
    expect(screen.queryByRole('button', { name: /awaiting approval/i })).not.toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Session scope' })).toBeInTheDocument()
  })

  it('keeps the daemon indicator in both scopes', () => {
    daemonHealthData.mockReturnValue(daemonHealth({ uptimeSec: 7200 }))
    renderColumn({ sessions: [session()], scope: 'archived' })
    expect(screen.getByTestId('daemon-health')).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm --prefix web test -- ScopeSegment SessionListColumn`
Expected: FAIL — `ScopeSegment` unresolved; `scope` is not a prop.

- [ ] **Step 3: Write `ScopeSegment`**

```tsx
// web/src/features/sessions/ScopeSegment.tsx
//
// Active | Archived — the sessions list's scope switch.
//
// It holds NO state. Scope is the route (`listScopeForPath`), so this is two
// links and a highlight: the Back button, a bookmark and a reload all land on
// the scope the URL names, and there is no second opinion to drift.
//
// Deliberately links rather than `role="tab"`: these navigate, and tab
// semantics would promise a tabpanel relationship that the router does not
// provide. `aria-current="page"` is the honest marker for "you are here".
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { ARCHIVED_PATH, SESSIONS_PATH } from '@/session/sessionNavState'

interface ScopeSegmentProps {
  scope: 'live' | 'archived'
  activeCount: number
  archivedCount: number
}

const item = cn(
  'flex-1 inline-flex min-h-target items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)

function count(n: number) {
  // A zero count renders nothing rather than "0" — the same convention the nav
  // badges use, and "Archived 0" reads as a broken counter.
  return n > 0 ? <span className="tabular-nums text-xs">{n}</span> : null
}

export function ScopeSegment({ scope, activeCount, archivedCount }: ScopeSegmentProps) {
  return (
    <nav
      aria-label="Session scope"
      className="flex gap-1 rounded-md border border-edge bg-surface p-1"
    >
      <Link
        to={SESSIONS_PATH}
        aria-current={scope === 'live' ? 'page' : undefined}
        className={cn(item, scope === 'live'
          ? 'bg-accent-wash text-accent'
          : 'text-ink-muted hover:bg-surface-raised hover:text-ink')}
      >
        Active
        {count(activeCount)}
      </Link>
      <Link
        to={ARCHIVED_PATH}
        aria-current={scope === 'archived' ? 'page' : undefined}
        className={cn(item, scope === 'archived'
          ? 'bg-accent-wash text-accent'
          : 'text-ink-muted hover:bg-surface-raised hover:text-ink')}
      >
        Archived
        {count(archivedCount)}
      </Link>
    </nav>
  )
}
```

- [ ] **Step 4: Write `useArchivedListState`**

```ts
// web/src/features/sessions/useArchivedListState.ts
//
// The archived scope's state, mirroring `useSessionListState` for the live one.
//
// It is deliberately NOT merged with that hook, and the two queries are never
// merged either: the live list polls every 3–5s and is reconciled over the
// WebSocket, while the archived list refetches every 30s and can hold hundreds
// of rows (there is a purge-the-oldest-200 button). Folding archived rows into
// the live stream would drag all of them through the hot poll for nothing.
//
// It is also thinner on purpose. There is no `nav` cursor: the prompt bar drives
// the LIVE list, and an archived row has nothing to dispatch to. There are no
// Status/Project/Environment filters: `ArchivedSession` carries no status, host
// or PR to filter on.
import { useArchivedSessions } from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'
import { useSessionSearch, type SessionSearch } from '@/session/useSessionSearch'
import { useItemOrdering, type OrderingHandle } from '@/session/useSessionOrdering'
import { ARCHIVED_SPEC, type ArchGroupBy, type ArchSortBy } from '@/session/archivedOrdering'

export interface ArchivedListState {
  /** Every archived session, unfiltered — what the controls strip is gated on. */
  sessions: readonly ArchivedSession[]
  search: SessionSearch
  ordering: OrderingHandle<ArchGroupBy, ArchSortBy, ArchivedSession>
}

export function useArchivedListState(): ArchivedListState {
  const { data: sessions = [] } = useArchivedSessions()
  const search = useSessionSearch('archived')
  const ordering = useItemOrdering(sessions, ARCHIVED_SPEC, search.matchedIds)
  return { sessions, search, ordering }
}
```

**Note for Task 9's list:** `ArchivedSessionList` currently calls `useArchivedSessions`, `useSessionSearch` and `useItemOrdering` itself. Change it to accept `state: ArchivedListState` as a prop and delete those three calls, so the column can render the controls strip in its fixed head from the same handle the list renders rows from. This mirrors exactly how `SessionListColumn` and `SessionList` already split `useSessionListState`.

- [ ] **Step 5: Rewrite `SessionListColumn`**

Split the body into a shared frame and two scope bodies, so no hook is called conditionally:

```tsx
export interface SessionListColumnProps {
  /** Which scope the route is in (`listScopeForPath`). */
  scope: 'live' | 'archived'
  state: SessionListState
  accounts: Account[]
  isLoading?: boolean
  selectedId: string | null
  newSelected?: boolean
  adoptionEnabled?: boolean
  listMode?: boolean
  onNew: () => void
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
  onDelete: (id: string) => void
  onQuickResume: (id: string, text?: string) => void
  /** Open an archived session's read-only transcript. */
  onReviewSession: (id: string) => void
  onExitList?: () => void
}

export function SessionListColumn({ scope, state, onNew, newSelected = false, onReviewSession, ...rest }: SessionListColumnProps) {
  // The archived COUNT only — the same query the deleted nav badge read, so
  // this is a cache hit, not a second fetch. The rows are fetched by
  // `ArchivedScopeBody`, and only while that scope is on screen.
  const { data: archived = [] } = useArchivedSessions()

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-2 p-2">
        {scope === 'live' && (
          <button
            type="button"
            data-selected={newSelected}
            onClick={onNew}
            className={/* …the existing New Session classes, verbatim… */}
          >
            <Plus size={16} aria-hidden="true" />
            New Session
          </button>
        )}
        <ScopeSegment scope={scope} activeCount={state.sessions.length} archivedCount={archived.length} />
      </div>

      {scope === 'live'
        ? <LiveScopeBody state={state} onNew={onNew} {...rest} />
        : <ArchivedScopeBody onReviewSession={onReviewSession} />}

      <div className="shrink-0 px-2 pb-2 empty:hidden">
        <DaemonHealthIndicator />
      </div>
    </div>
  )
}
```

Each scope body is a fragment contributing exactly two flex children — a `shrink-0` controls strip and the one scrolling band — so the column's layout is unchanged in either scope:

```tsx
/** The live scope: today's column body, moved verbatim. */
function LiveScopeBody({ state, accounts, isLoading, selectedId, adoptionEnabled, listMode, onNew, onOpen, onRename, onClose, onDelete, onQuickResume, onExitList }: …) {
  const { sessions, queued, pendingApproval, search, ordering, nav } = state
  const { filters, setFilters, filterOptions } = state
  const filtered = hasActiveFilters(filters)
  return (
    <>
      <div className="shrink-0 space-y-2 px-2">
        {sessions.length > 0 && (
          <SessionListControls /* …existing props, verbatim… */ />
        )}
      </div>
      <div data-testid="session-list-scroll" className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2">
        <PendingApprovalSection sessions={pendingApproval} onOpen={onOpen} />
        <QueuedSection sessions={queued} onOpen={onOpen} />
        <SessionList /* …existing props, verbatim… */ />
        {adoptionEnabled && <AdoptPanel />}
      </div>
    </>
  )
}

/** The archived scope. Its hook lives here, so it runs only while this scope is
 *  mounted — the 30 s archived poll does not run behind the live list. */
function ArchivedScopeBody({ onReviewSession }: { onReviewSession: (id: string) => void }) {
  const state = useArchivedListState()
  return (
    <>
      <div className="shrink-0 space-y-2 px-2">
        {state.sessions.length > 0 && (
          <SessionListControls
            groupBy={state.ordering.groupBy}
            sortBy={state.ordering.sortBy}
            onGroupByChange={state.ordering.setGroupBy}
            onSortByChange={state.ordering.setSortBy}
            groupOptions={ARCHIVED_GROUP_OPTIONS}
            sortOptions={ARCHIVED_SORT_OPTIONS}
            searchQuery={state.search.query}
            onSearchChange={state.search.setQuery}
            searchPlaceholder="Search archived…"
            searchAriaLabel="Search archived sessions"
          />
        )}
      </div>
      <div data-testid="session-list-scroll" className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2">
        <ArchivedSessionList state={state} onReviewSession={onReviewSession} />
      </div>
    </>
  )
}
```

`ArchivedSessionList` loses its own controls strip (the column head owns it now) and its `<h2>Archived Sessions</h2>` (the column is already named by `ContextList`'s `sr-only` heading).

- [ ] **Step 6: Wire the shell**

In `AppShell.tsx`'s `useSessionsWorkspace`, add to `listProps`:

```tsx
    scope: listScopeForPath(pathname),
    onReviewSession: (id: string) => navigate(archivedSessionPath(id)),
```

…importing `listScopeForPath` and `archivedSessionPath`, and reading `pathname` from the `useLocation()` the hook already has (or adding one).

- [ ] **Step 7: Fix the test mocks the new hook breaks**

`SessionListColumn` now calls `useArchivedSessions`. Suites that hand-mock `@/api/hooks` will get `undefined` back and crash on `.data`. Add `useArchivedSessions: vi.fn(() => queryResult([]))` to every such mock that mounts the column — grep for `vi.mock('@/api/hooks'` and check each hit. `SessionListColumn.test.tsx` uses a partial mock (`importOriginal`), so add `useArchivedSessions` to the returned override there rather than letting the real query fire.

This is the exact failure mode from PR #938. **Run the full suite, not the changed files.**

- [ ] **Step 8: Run the full suite and the gates, then commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/features/sessions/ web/src/components/ArchivedSessionList.tsx web/src/shell/AppShell.tsx web/src/components/__tests__/
git commit -F - <<'MSG'
feat(web): Active/Archived scope switch inside the sessions list

The column renders one scope or the other from the route. The archived query
runs only while its scope is mounted, so the live list's poll is untouched.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

---

### Task 14: Delete the `archived` nav section

**Files:**
- Modify: `web/src/shell/navDefs.ts:97-102` + the `NavBadgeKey` union (line ~8) + the `sessions` help text (line 78), `web/src/shell/useNavBadges.ts`
- Modify: `web/src/shell/navDefs.test.ts`, `useNavBadges.test.tsx`, `regions/NavRail.test.tsx`, `routes.test.tsx`, `kiosk.integration.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 11–13.
- Produces: `NavId` loses `'archived'`; `NavBadgeKey` loses `'archived'`; `NavBadges` loses its `archived` field.

- [ ] **Step 1: Update the tests first — they are the specification here**

`navDefs.test.ts`: remove `['archived', 'Archived']` from the order/label list; remove `'archived'` from the `known` badge-key set.

`useNavBadges.test.tsx`: delete `expect(result.current.archived).toBeUndefined()` from the zero-count case; in the "pipeline counts in-flight PRs" case drop the `useArchivedSessions` stub and the `archived` assertion, and rename the case to "pipeline counts in-flight PRs; automations counts its own data". Delete the now-unused local `archivedSession` factory and its `ArchivedSession` import.

`NavRail.test.tsx`: remove `archived: undefined,` from the `navBadges` factory.

`routes.test.tsx`: in the "owns a vertical scroll container" `it.each`, replace `'/archived'` with `'/sessions/archived'`. In the kiosk-refused expectation list, replace `'/archived', '/archived/abc'` with `'/sessions/archived', '/sessions/archived/abc'`. The matrix stays at 44 cells — two paths were renamed, none added or removed.

`kiosk.integration.test.tsx:335-350`: update the docstring — the example URL becomes `/sessions/archived?kiosk=1`, and "`ArchivedManager`'s Permanent Delete" becomes "the archived scope's Permanent Delete".

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npm --prefix web test -- navDefs useNavBadges NavRail routes kiosk`
Expected: FAIL — `NAV_DEFS` still contains the archived entry; `useNavBadges` still returns `archived`.

- [ ] **Step 3: Delete the nav entry and the badge**

In `navDefs.ts`, delete the whole `archived` row and its comment block, and remove `'archived'` from `NavBadgeKey`. Update the `sessions` row's help text so the scope is discoverable from the help sheet:

```ts
  { id: 'sessions',    label: 'Sessions',    icon: MessagesSquare,  path: '/sessions',    badge: 'sessions',    showInKiosk: false, hasList: true,
    help: 'Live sessions. Start one from the prompt; open one to read its transcript and reply. Switch the list to Archived to reread, restore or delete a past session.' },
```

`Archive` may become an unused lucide import — remove it if so.

In `useNavBadges.ts`, delete the `useArchivedSessions` import, its call, and the `archived` field; update the docstring to say the archived count now lives on the list's own scope switch.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm --prefix web test -- navDefs useNavBadges NavRail routes kiosk`
Expected: PASS.

- [ ] **Step 5: Sweep for stragglers**

```bash
grep -rn "'/archived\|\"/archived\|NAV_BY_ID.archived\|badges.archived" web/src --include=*.ts --include=*.tsx
```

Expected: only `LEGACY_ARCHIVED_PATH` / `LEGACY_ARCHIVED_SESSION_ROUTE` in `sessionNavState.ts` and their two uses in `routes.tsx` and `routes.test.tsx`. Anything else is a missed call site.

- [ ] **Step 6: Update the two design docs the spec names**

In `docs/audits/2026-09-02-ui-framework-audit.md`, annotate the Archived rows (lines ~275 and ~350): Archived is a SCOPE of the Sessions list as of this change, not a Standard page; the reader is still Reader.

In `docs/superpowers/plans/2026-09-03-ui-shell-appshell.md`, annotate DEC-6 (line ~65): the review route is `/sessions/archived/:archivedId` as of 2026-09-04, superseded by D2 of the archived-scope design. **Annotate, do not rewrite** — a plan is a record of what was decided when.

- [ ] **Step 7: Run the full suite and the gates, then commit**

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
git add web/src/shell/ docs/
git commit -F - <<'MSG'
refactor(shell): Archived leaves the nav — it is a scope of the Sessions list

The section, its badge and its badge key are deleted; the count moved onto the
list's scope switch and the Sessions help text now names the scope.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
git push
```

**PR 3 ends here.** Visible change: Archived is gone from the nav and lives inside the Sessions list.

---

## Self-review

**Spec coverage.** D1 → Task 11 (`listScopeForPath`, no param/storage). D2 → Task 11 (`:archivedId`, reader under `/sessions`). D3 → Task 12 (`useOpenItem`). D4 → Task 13 (`useArchivedListState`, separate queries). D5 → Task 8 (distinct type, no status-union change). D6 → Task 9 (quick purge, progress, select-all, Review stay archived-only). D7 → Task 11 (redirects carrying search; `ARCHIVED_PATH`/`archivedSessionPath` keep their names). D8 → Task 10. Spec §4.4 → Tasks 1–5. §4.5 → Tasks 6–8. §4.6 → Task 10. §7's moved/new/updated tests → distributed across every task. §9's doc updates → Task 14 Step 6.

**One spec item deliberately deferred, and why.** §6 proposes a new error state for `useArchivedSessions` failing ("Archived sessions unavailable" with a retry). No task implements it: the archived query has no error state today, `NoMatchState` is not an error component, and adding one touches the query's `isError` plumbing rather than this consolidation. **Open it as a follow-up issue** when PR 3 lands; do not smuggle it into Task 13.

**Known sharp edges for the implementer.**
- Task 5 and Task 7 are *extractions*. If an existing assertion fails, the extraction is wrong. Never edit the assertion to match new behaviour — the only sanctioned test edits are the ones each task names explicitly.
- Task 13 Step 7 is the step most likely to be skipped and most likely to redden CI. Grep for every `vi.mock('@/api/hooks'` before running.
- `ArchiveRestore` (Task 8) must exist in the installed lucide-react. If it does not, use `Archive` with a distinct `aria-label` rather than adding a dependency.
