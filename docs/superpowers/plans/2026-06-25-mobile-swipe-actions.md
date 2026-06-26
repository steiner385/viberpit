# Mobile Swipe Actions for Session Cards — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mobile users swipe a session card left to Delete or right to Archive, with a hybrid reveal-then-tap / full-swipe-commits interaction.

**Architecture:** Pure gesture math (`swipeMath.ts`) → a session-agnostic Pointer-Event hook (`useSwipeAction.ts`) → a mobile-only swipe layer rendered inside `SessionCard`. The hook follows the existing `SidebarResizer` pattern (window-attached `pointermove`/`pointerup` listeners, no `setPointerCapture`) so it works in jsdom. Archive reuses `onClose`; Delete reuses `onDelete`, which already opens `PermanentDeleteDialog`. Desktop (`>= lg`) is untouched.

**Tech Stack:** React + TypeScript, Tailwind, Vitest + @testing-library/react (jsdom), `lucide-react` icons. No new dependency.

## Global Constraints

- No new npm dependency (constitution VII; hand-rolled Pointer Events only).
- Mobile gate is `useMediaQuery('(min-width: 1024px)')` from `web/src/lib/useMediaQuery.ts` — the app's existing `lg` definition (used in `App.tsx`). Swipe is active only when this returns `false`. In jsdom it returns `false` (mobile) by default.
- Direction mapping: **swipe left → Delete** (card slides left, red panel revealed on the right edge); **swipe right → Archive** (card slides right, panel revealed on the left edge).
- Delete always routes through the existing confirm flow (`onDelete(id)` → `PermanentDeleteDialog`); never delete without it. Archive (`onClose(id)`) commits directly.
- Swipe-panel button accessible names MUST be `Archive ${session.name}` and `Delete ${session.name}` (NO the word "session") to avoid colliding with the desktop buttons' `Archive session ${name}` / `Delete session ${name}` names that existing tests query with `/archive session/i` and `/delete session/i`.
- Run web checks from `web/`: `npx vitest run`, `npx tsc -b --force` (the real type gate — `tsc --noEmit` checks nothing here), `npx eslint . --max-warnings 0`.
- Worktree test env: if `node_modules` is missing/stale, `rm -f node_modules && npm ci` (registry reachable, lockfile committed).

---

### Task 1: Pure gesture math (`swipeMath.ts`)

Pure functions with no DOM dependency: the axis-lock decision and the release-settle decision. These carry the thresholds and are exhaustively unit-tested.

**Files:**
- Create: `web/src/lib/swipeMath.ts`
- Test: `web/src/lib/__tests__/swipeMath.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `type SwipeSide = 'left' | 'right'`
  - `type SwipeOutcome = 'closed' | 'open' | 'commit'`
  - `function shouldLockHorizontal(dx: number, dy: number, bias?: number): boolean`
  - `interface SettleInput { dx: number; width: number; revealPx: number; commitFraction: number }`
  - `function settleSwipe(input: SettleInput): { outcome: SwipeOutcome; side: SwipeSide | null }`

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/swipeMath.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { shouldLockHorizontal, settleSwipe } from '../swipeMath'

describe('shouldLockHorizontal', () => {
  it('locks when horizontal leads vertical by more than the bias', () => {
    expect(shouldLockHorizontal(20, 0)).toBe(true)
    expect(shouldLockHorizontal(-20, 3)).toBe(true)
  })
  it('does not lock for a vertical-dominant or ambiguous move', () => {
    expect(shouldLockHorizontal(2, 30)).toBe(false) // clearly vertical
    expect(shouldLockHorizontal(5, 0)).toBe(false)  // within default bias (8)
  })
})

describe('settleSwipe', () => {
  const base = { width: 300, revealPx: 64, commitFraction: 0.45 } // commit at 135px

  it('commits left when dragged left past the commit fraction', () => {
    expect(settleSwipe({ dx: -140, ...base })).toEqual({ outcome: 'commit', side: 'left' })
  })
  it('commits right when dragged right past the commit fraction', () => {
    expect(settleSwipe({ dx: 150, ...base })).toEqual({ outcome: 'commit', side: 'right' })
  })
  it('opens (reveals) between the reveal and commit thresholds', () => {
    expect(settleSwipe({ dx: -80, ...base })).toEqual({ outcome: 'open', side: 'left' })
    expect(settleSwipe({ dx: 80, ...base })).toEqual({ outcome: 'open', side: 'right' })
  })
  it('closes below the reveal threshold', () => {
    expect(settleSwipe({ dx: -40, ...base })).toEqual({ outcome: 'closed', side: null })
    expect(settleSwipe({ dx: 0, ...base })).toEqual({ outcome: 'closed', side: null })
  })
  it('never commits when width is unknown (0) — only reveal/close', () => {
    expect(settleSwipe({ dx: -500, width: 0, revealPx: 64, commitFraction: 0.45 }))
      .toEqual({ outcome: 'open', side: 'left' })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/lib/__tests__/swipeMath.test.ts`
Expected: FAIL — "Failed to resolve import '../swipeMath'".

- [ ] **Step 3: Write minimal implementation**

Create `web/src/lib/swipeMath.ts`:

```ts
/** Which edge action a settled swipe targets. */
export type SwipeSide = 'left' | 'right'

/** Resting state a released swipe resolves to. */
export type SwipeOutcome = 'closed' | 'open' | 'commit'

/**
 * One axis "wins" only when it leads the other by more than `bias` px, so a
 * near-diagonal drag stays ambiguous (caller keeps waiting) instead of
 * flickering between scroll and swipe. Reused for the vertical test by swapping
 * the arguments: `shouldLockHorizontal(dy, dx)`.
 */
export function shouldLockHorizontal(dx: number, dy: number, bias = 8): boolean {
  return Math.abs(dx) > Math.abs(dy) + bias
}

export interface SettleInput {
  /** Signed horizontal offset at release; negative = swiped left. */
  dx: number
  /** Card width in px. 0 when unknown (e.g. unmeasured in jsdom). */
  width: number
  /** Distance to snap the card open (reveal the button). */
  revealPx: number
  /** Fraction of card width past which a release commits directly. */
  commitFraction: number
}

/**
 * Resolve a released drag to a resting outcome + side. A zero width disables
 * direct commit (we never fire a destructive action off an unmeasured card).
 */
export function settleSwipe({ dx, width, revealPx, commitFraction }: SettleInput): {
  outcome: SwipeOutcome
  side: SwipeSide | null
} {
  const side: SwipeSide | null = dx < 0 ? 'left' : dx > 0 ? 'right' : null
  if (!side) return { outcome: 'closed', side: null }
  const dist = Math.abs(dx)
  if (width > 0 && dist >= width * commitFraction) return { outcome: 'commit', side }
  if (dist >= revealPx) return { outcome: 'open', side }
  return { outcome: 'closed', side: null }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/lib/__tests__/swipeMath.test.ts`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/swipeMath.ts web/src/lib/__tests__/swipeMath.test.ts
git commit -m "feat(web): pure swipe gesture math (axis lock + settle)"
```

---

### Task 2: The `useSwipeAction` hook

Wires Pointer Events to the math. Session-agnostic: takes `enabled` + `onCommit(side)`, manages drag/open state, and exposes handlers + a `consumeDrag()` guard so a swipe's trailing click does not also open the session.

**Files:**
- Create: `web/src/lib/useSwipeAction.ts`
- Test: `web/src/lib/__tests__/useSwipeAction.test.tsx`

**Interfaces:**
- Consumes (Task 1): `settleSwipe`, `shouldLockHorizontal`, `SwipeSide`.
- Produces:
  - `interface UseSwipeActionOptions { enabled: boolean; onCommit: (side: SwipeSide) => void; revealPx?: number; commitFraction?: number }`
  - `interface SwipeApi { dx: number; openSide: SwipeSide | null; dragging: boolean; onPointerDown: (e: React.PointerEvent<HTMLElement>) => void; reset: () => void; consumeDrag: () => boolean }`
  - `function useSwipeAction(opts: UseSwipeActionOptions): SwipeApi`
  - Exported constants `SWIPE_REVEAL_PX = 64`, `SWIPE_COMMIT_FRACTION = 0.45`.

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/useSwipeAction.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { SwipeSide } from '../swipeMath'
import { useSwipeAction } from '../useSwipeAction'

function Probe({ onCommit, enabled = true }: { onCommit: (s: SwipeSide) => void; enabled?: boolean }) {
  const swipe = useSwipeAction({ enabled, onCommit })
  return (
    <div data-testid="fg" onPointerDown={swipe.onPointerDown}>
      <span data-testid="open">{swipe.openSide ?? 'none'}</span>
      <span data-testid="dragging">{String(swipe.dragging)}</span>
      <button onClick={() => { if (!swipe.consumeDrag()) onCommit('right') /* stand-in for "open" */ }}>
        open
      </button>
    </div>
  )
}

/** jsdom never measures layout, so pin the card width the hook reads. */
function widthed(el: HTMLElement, px: number) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: px })
}

describe('useSwipeAction', () => {
  it('commits left when a horizontal drag passes the commit fraction', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300) // commit at 135px
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 60, clientY: 55 }) // dx -140, horizontal
    fireEvent.pointerUp(window, { clientX: 60, clientY: 55 })
    expect(onCommit).toHaveBeenCalledWith('left')
  })

  it('reveals (opens) a side on a short horizontal drag without committing', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 120, clientY: 52 }) // dx -80: reveal, not commit
    fireEvent.pointerUp(window, { clientX: 120, clientY: 52 })
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByTestId('open').textContent).toBe('left')
  })

  it('yields to vertical scroll: a vertical-dominant drag never engages', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 205, clientY: 200 }) // dy 150 >> dx 5
    fireEvent.pointerUp(window, { clientX: 205, clientY: 200 })
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByTestId('dragging').textContent).toBe('false')
    expect(screen.getByTestId('open').textContent).toBe('none')
  })

  it('consumeDrag suppresses the click that trails a real drag, once', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    // Short horizontal drag that settles closed but DID move horizontally.
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 51 }) // dx -30: closed, but a drag
    fireEvent.pointerUp(window, { clientX: 170, clientY: 51 })
    fireEvent.click(screen.getByText('open'))
    expect(onCommit).not.toHaveBeenCalled() // first click swallowed
    fireEvent.click(screen.getByText('open'))
    expect(onCommit).toHaveBeenCalledWith('right') // flag cleared; second click acts
  })

  it('does nothing when disabled', () => {
    const onCommit = vi.fn()
    render(<Probe onCommit={onCommit} enabled={false} />)
    const fg = screen.getByTestId('fg'); widthed(fg, 300)
    fireEvent.pointerDown(fg, { clientX: 200, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 60, clientY: 55 })
    fireEvent.pointerUp(window, { clientX: 60, clientY: 55 })
    expect(onCommit).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/lib/__tests__/useSwipeAction.test.tsx`
Expected: FAIL — "Failed to resolve import '../useSwipeAction'".

- [ ] **Step 3: Write minimal implementation**

Create `web/src/lib/useSwipeAction.ts`:

```ts
import { useCallback, useRef, useState } from 'react'
import { settleSwipe, shouldLockHorizontal, type SwipeSide } from './swipeMath'

export const SWIPE_REVEAL_PX = 64
export const SWIPE_COMMIT_FRACTION = 0.45

export interface UseSwipeActionOptions {
  /** When false, the gesture is inert (desktop). */
  enabled: boolean
  /** Fired when a drag commits an edge action. */
  onCommit: (side: SwipeSide) => void
  revealPx?: number
  commitFraction?: number
}

export interface SwipeApi {
  /** Live horizontal offset while dragging (px). 0 when at rest. */
  dx: number
  /** Side currently held open (revealed), or null. */
  openSide: SwipeSide | null
  dragging: boolean
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void
  /** Snap closed (e.g. after the revealed button is tapped). */
  reset: () => void
  /** True (once) if the last pointer interaction was a real horizontal drag,
   *  so the trailing click should be ignored rather than opening the session. */
  consumeDrag: () => boolean
}

export function useSwipeAction({
  enabled,
  onCommit,
  revealPx = SWIPE_REVEAL_PX,
  commitFraction = SWIPE_COMMIT_FRACTION,
}: UseSwipeActionOptions): SwipeApi {
  const [dx, setDx] = useState(0)
  const [openSide, setOpenSide] = useState<SwipeSide | null>(null)
  const [dragging, setDragging] = useState(false)
  const g = useRef({ startX: 0, startY: 0, width: 0, locked: false, active: false, didDrag: false })

  const reset = useCallback(() => { setDx(0); setOpenSide(null) }, [])

  const consumeDrag = useCallback(() => {
    const v = g.current.didDrag
    g.current.didDrag = false
    return v
  }, [])

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (!enabled) return
    const s = g.current
    s.startX = e.clientX
    s.startY = e.clientY
    s.width = e.currentTarget.offsetWidth
    s.locked = false
    s.active = true
    s.didDrag = false

    const cleanup = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
    }
    function onMove(ev: PointerEvent) {
      if (!s.active) return
      const ddx = ev.clientX - s.startX
      const ddy = ev.clientY - s.startY
      if (!s.locked) {
        if (shouldLockHorizontal(ddx, ddy)) {
          s.locked = true
          setDragging(true)
        } else if (shouldLockHorizontal(ddy, ddx)) {
          // Vertical intent — yield to the list's native scroll, abandon gesture.
          s.active = false
          cleanup()
          return
        } else {
          return // still ambiguous; wait for more travel
        }
      }
      s.didDrag = true
      setDx(ddx)
    }
    function onUp(ev: PointerEvent) {
      if (!s.active) { cleanup(); return }
      s.active = false
      setDragging(false)
      const finalDx = ev.clientX - s.startX
      const { outcome, side } = settleSwipe({ dx: finalDx, width: s.width, revealPx, commitFraction })
      if (outcome === 'commit' && side) { setDx(0); setOpenSide(null); onCommit(side) }
      else if (outcome === 'open' && side) { setDx(0); setOpenSide(side) }
      else { setDx(0); setOpenSide(null) }
      cleanup()
    }
    function onCancel() {
      s.active = false
      setDragging(false)
      setDx(0)
      cleanup()
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
  }, [enabled, onCommit, revealPx, commitFraction])

  return { dx, openSide, dragging, onPointerDown, reset, consumeDrag }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/lib/__tests__/useSwipeAction.test.tsx`
Expected: PASS (all five cases).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/useSwipeAction.ts web/src/lib/__tests__/useSwipeAction.test.tsx
git commit -m "feat(web): useSwipeAction pointer-event hook"
```

---

### Task 3: Mobile swipe layer in `SessionCard`

Render the swipe scaffolding only on mobile (`!isWide`): two edge action panels (Archive left, Delete right) behind a translatable foreground that holds the existing card row. Wire commits to `onClose`/`onDelete`, suppress the open-tap after a drag, and honor `prefers-reduced-motion`.

**Files:**
- Modify: `web/src/components/SessionCard.tsx`
- Test: `web/src/components/__tests__/SessionCardSwipe.test.tsx`

**Interfaces:**
- Consumes (Task 2): `useSwipeAction`, `SwipeApi`.
- Consumes (existing): `useMediaQuery` (`web/src/lib/useMediaQuery.ts`), `SessionCardProps.onClose`, `SessionCardProps.onDelete`, `SessionCardProps.onOpen`.
- Produces: a swipe foreground element with `data-testid="card-swipe-fg"`; panel buttons named `Archive ${name}` / `Delete ${name}`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/SessionCardSwipe.test.tsx`:

```tsx
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const SESSION = { id: 's1', name: 'Alpha', accountName: 'acct', status: 'ready', updatedAt: null, subAgents: [], latestSuggestions: [], prs: [] } as unknown as Session

function props(over = {}) {
  return {
    session: SESSION, account: undefined,
    onOpen: vi.fn(), onRename: vi.fn(), onClose: vi.fn(), onDelete: vi.fn(),
    onQuickResume: vi.fn(), ...over,
  }
}

function fg() {
  const el = screen.getByTestId('card-swipe-fg')
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 }) // commit at 135px
  return el
}

afterEach(() => { vi.restoreAllMocks() })

describe('SessionCard — mobile swipe', () => {
  it('full left-swipe commits Delete (onDelete)', () => {
    const onDelete = vi.fn()
    render(<SessionCard {...props({ onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 100, clientY: 45 }) // dx -150
    fireEvent.pointerUp(window, { clientX: 100, clientY: 45 })
    expect(onDelete).toHaveBeenCalledWith('s1')
  })

  it('full right-swipe commits Archive (onClose)', () => {
    const onClose = vi.fn()
    render(<SessionCard {...props({ onClose })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 }) // dx +160
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onClose).toHaveBeenCalledWith('s1')
  })

  it('short left-swipe reveals a tappable Delete button', () => {
    const onDelete = vi.fn()
    render(<SessionCard {...props({ onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 }) // dx -80: reveal
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alpha' }))
    expect(onDelete).toHaveBeenCalledWith('s1')
  })

  it('a vertical drag neither commits nor opens the session', () => {
    const onOpen = vi.fn(), onClose = vi.fn(), onDelete = vi.fn()
    render(<SessionCard {...props({ onOpen, onClose, onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 200, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 205, clientY: 200 }) // vertical
    fireEvent.pointerUp(window, { clientX: 205, clientY: 200 })
    expect(onClose).not.toHaveBeenCalled()
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('a horizontal drag suppresses the trailing open-tap', () => {
    const onOpen = vi.fn()
    render(<SessionCard {...props({ onOpen })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 200, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 168, clientY: 41 }) // small horizontal drag
    fireEvent.pointerUp(window, { clientX: 168, clientY: 41 })
    fireEvent.click(screen.getByRole('button', { name: /open session alpha/i }))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('does not render the swipe layer at desktop (lg) width', () => {
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q.includes('min-width: 1024px'), media: q,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia
    render(<SessionCard {...props()} />)
    expect(screen.queryByTestId('card-swipe-fg')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete Alpha' })).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/__tests__/SessionCardSwipe.test.tsx`
Expected: FAIL — `card-swipe-fg` testid not found.

- [ ] **Step 3: Implement the swipe layer in `SessionCard.tsx`**

3a. Add imports near the top of `web/src/components/SessionCard.tsx` (the `lucide-react` line already imports `Archive` and `Trash2`):

```ts
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useSwipeAction } from '@/lib/useSwipeAction'
```

3b. Inside `SessionCard`, after the existing `const repoLabel = formatRepoLabel(session.repo)` line, add the gate + hook:

```ts
  // Touch-only swipe actions. lg = the app's desktop breakpoint (App.tsx); jsdom
  // reports false → mobile, so tests exercise this branch by default.
  const isWide = useMediaQuery('(min-width: 1024px)')
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const swipeEnabled = !isWide && !session.pending
  const swipe = useSwipeAction({
    enabled: swipeEnabled,
    // Swipe left → Delete (destructive, confirmed by onDelete's dialog).
    // Swipe right → Archive (reversible).
    onCommit: (side) => (side === 'left' ? onDelete(session.id) : onClose(session.id)),
  })
  // Resting offset: follow the finger while dragging, else rest open at the panel
  // width (negative = left/delete revealed), else closed.
  const PANEL_PX = 88
  const restX = swipe.dragging
    ? swipe.dx
    : swipe.openSide === 'left' ? -PANEL_PX : swipe.openSide === 'right' ? PANEL_PX : 0
```

3c. In the non-pending `return`, replace the opening `<article ...>` and its inner content wrapper so the existing card body becomes the swipe foreground. The current structure is:

```tsx
return (
  <article ref={articleRef} className={cn(/* ... */)}>
   <div className="flex items-center gap-2">
     {/* ...existing card body... */}
   </div>
    {hasSubAgents && expanded && <SubAgentRows .../>}
  </article>
)
```

Wrap the body in a relative clipping container, add the two panels, and move `onPointerDown` + the translate onto a foreground wrapper. New structure:

```tsx
return (
  <article
    ref={articleRef}
    className={cn(
      'group flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
      'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px]',
      'focus-within:ring-2 focus-within:ring-blue-500',
      selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
      !isWide && 'relative overflow-hidden', // clip the edge panels; mobile only so the desktop quick-reply menu is never clipped
    )}
  >
    {!isWide && (
      <>
        {/* Archive panel — left edge, revealed by swiping right (openSide==='right'). */}
        <button
          type="button"
          aria-label={`Archive ${session.name}`}
          tabIndex={swipe.openSide === 'right' ? 0 : -1}
          onClick={() => { swipe.reset(); onClose(session.id) }}
          className={cn(
            'absolute inset-y-0 left-0 flex w-[88px] items-center justify-center',
            'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
            swipe.openSide === 'right' ? 'pointer-events-auto' : 'pointer-events-none',
          )}
        >
          <Archive size={20} aria-hidden="true" />
        </button>
        {/* Delete panel — right edge, revealed by swiping left (openSide==='left'). */}
        <button
          type="button"
          aria-label={`Delete ${session.name}`}
          tabIndex={swipe.openSide === 'left' ? 0 : -1}
          onClick={() => { swipe.reset(); onDelete(session.id) }}
          className={cn(
            'absolute inset-y-0 right-0 flex w-[88px] items-center justify-center',
            'bg-red-600 text-white',
            swipe.openSide === 'left' ? 'pointer-events-auto' : 'pointer-events-none',
          )}
        >
          <Trash2 size={20} aria-hidden="true" />
        </button>
      </>
    )}

    {/* Foreground — the existing card body. On mobile it translates over the
        panels; on desktop it is a plain wrapper with no gesture handlers. */}
    <div
      {...(!isWide ? { 'data-testid': 'card-swipe-fg', onPointerDown: swipe.onPointerDown } : {})}
      style={!isWide ? { transform: `translateX(${restX}px)` } : undefined}
      className={cn(
        !isWide && 'relative bg-white dark:bg-gray-800 touch-pan-y',
        !isWide && !swipe.dragging && !reduceMotion && 'transition-transform duration-200',
      )}
    >
      <div className="flex items-center gap-2">
        {/* ...existing card body, UNCHANGED... */}
      </div>
      {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
    </div>
  </article>
)
```

Note: the inner `<div className="flex items-center gap-2">…</div>` and the `SubAgentRows` line are the existing markup moved verbatim inside the new foreground wrapper — do not edit their contents.

3d. Suppress the open-tap after a drag and let a tap close an open card. Edit the main tappable `<button id={`row-${session.id}`} … onClick={() => onOpen(session.id)}>` so its `onClick` becomes:

```tsx
        onClick={() => {
          if (swipe.openSide) { swipe.reset(); return }   // tap closes a revealed card
          if (swipe.consumeDrag()) return                  // swallow the click that ends a drag
          onOpen(session.id)
        }}
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `cd web && npx vitest run src/components/__tests__/SessionCardSwipe.test.tsx`
Expected: PASS (all six cases).

- [ ] **Step 5: Run the existing SessionCard/list/app suites for regressions**

Run: `cd web && npx vitest run src/components/__tests__/SessionCard.test.tsx src/components/__tests__/SessionCard.layout.test.tsx src/components/__tests__/SessionList.test.tsx src/components/__tests__/SessionActions.test.tsx src/components/__tests__/DesktopSessionActions.test.tsx src/App.test.tsx`
Expected: PASS. If any fail on a duplicate accessible name, confirm the swipe panels use `Archive ${name}` / `Delete ${name}` (NOT "session") per the Global Constraints — do not rename the desktop buttons.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCardSwipe.test.tsx
git commit -m "feat(web): swipe-to-archive/delete on mobile session cards"
```

---

### Task 4: Full verification

Run the project's real web gates (the only true type gate is `tsc -b`) and the whole vitest suite (CI runs the full suite; subset-green can hide fixture breakage).

**Files:** none (verification only).

- [ ] **Step 1: Type-check**

Run: `cd web && npx tsc -b --force`
Expected: no errors.

- [ ] **Step 2: Lint (CI uses max-warnings 0)**

Run: `cd web && npx eslint . --max-warnings 0`
Expected: no errors/warnings. Watch for react-hooks rules (no ref read during render — the hook reads refs only inside event handlers, which is fine).

- [ ] **Step 3: Full test suite**

Run: `cd web && npx vitest run`
Expected: all pass.

- [ ] **Step 4: Commit any fixups**

```bash
git add -A
git commit -m "chore(web): verification fixups for mobile swipe actions" || echo "nothing to fix up"
```

---

## Self-Review

**Spec coverage:**
- Hybrid reveal + full-swipe commit → Task 1 `settleSwipe` (`open` vs `commit`), Task 2 hook state, Task 3 panels + commit wiring. ✓
- Left = Delete, Right = Archive → Task 3 `onCommit` mapping + panel placement. ✓
- Delete always confirms → Task 3 calls `onDelete` (parent opens `PermanentDeleteDialog`, unchanged). ✓
- Archive commits directly → Task 3 `onClose`. ✓
- Active list, mobile only → `swipeEnabled = !isWide`, panels gated on `!isWide`, Task 3 desktop test. ✓
- Axis-lock / yield to scroll → Task 1 `shouldLockHorizontal` + Task 2 vertical-abort + `touch-pan-y`. ✓
- Accessibility (real labelled buttons, keyboard untouched) → Task 3 panel buttons; list-mode keys untouched. ✓
- Reduced motion → Task 3 `reduceMotion` gates the transition. ✓
- No new dependency → hand-rolled; verified by Task 4. ✓

**Placeholder scan:** none — every code step is complete; the only "...existing card body..." marker is an explicit instruction to leave verbatim markup in place, not a fill-in.

**Type consistency:** `SwipeSide`, `settleSwipe`, `shouldLockHorizontal`, `useSwipeAction`, `SwipeApi.{dx,openSide,dragging,onPointerDown,reset,consumeDrag}` are named identically across Tasks 1–3. Panel button names `Archive ${name}` / `Delete ${name}` match the Task 3 test queries.
