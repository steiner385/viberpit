import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { pushOverlay, popOverlay } from '@/shell/useOverlayState'
import { Kbd } from './Kbd'

export interface MenuItem {
  id: string
  label: React.ReactNode
  shortcut?: string
  icon?: React.ReactNode
  disabled?: boolean
  selected?: boolean
}

export interface MenuProps {
  open: boolean
  onClose: () => void
  anchorRef: React.RefObject<HTMLElement | null>
  items: MenuItem[]
  onSelect: (id: string) => void
  /** Accessible name of the menu. */
  label: string
  align?: 'start' | 'end'
  className?: string
}

const GAP = 4

export function Menu({ open, onClose, anchorRef, items, onSelect, label, align = 'start', className }: MenuProps) {
  const listRef = React.useRef<HTMLDivElement>(null)
  const [active, setActive] = React.useState(0)
  // Callers pass `items` as an inline array literal — the normal shape — so it
  // arrives with a fresh identity on every parent render. Memoizing off that
  // identity re-ran the position effect below, which resets `active` to the
  // first enabled item and drags DOM focus after it: arrow-key to "Plan", let a
  // background poll re-render the composer, and the highlight snaps back to
  // "Manual". Ruling R21 calls that fatal for the pickers this primitive exists
  // for; the same defect was fixed for `onClose` one effect away and missed
  // here. So stabilise on a *value* signature — each item's id plus its
  // disabled state, which is all enabledIdx reads — and a structurally
  // identical inline array counts as unchanged. A real change (an item
  // disabled, reordered, added, removed) still moves the key and recomputes.
  //
  // The signature is shaped so enabledIdx derives from it alone — one flag per
  // item ('1' = disabled), then the ids, which are present only so a reorder or
  // a same-length replacement still moves the key. Deriving from the string
  // keeps the memo dependent on exactly what it is keyed by, with no second,
  // unlisted read of `items`.
  const itemsKey =
    items.map((it) => (it.disabled ? '1' : '0')).join('') + '|' + items.map((it) => it.id).join(' ')
  // Single source of truth for "which items are selectable, in order": both
  // the digit shortcuts (enabledIdx[N-1]) and the rendered badge numbers
  // (enabledIdx.indexOf(i) + 1, below) index off this same array, so they
  // cannot disagree about which item is "Nth".
  const enabledIdx = React.useMemo(() => {
    const flags = itemsKey.slice(0, itemsKey.indexOf('|'))
    const out: number[] = []
    for (let i = 0; i < flags.length; i++) if (flags[i] === '0') out.push(i)
    return out
  }, [itemsKey])

  // Register as an open overlay while the menu is up. A Menu is not a focus
  // trap and renders no `role="dialog"`, so before this nothing outside this
  // file could tell it was open — and a global ArrowUp went straight past an
  // open menu to the session list underneath it.
  //
  // The push and the pop are the setup and cleanup of ONE effect, which is what
  // makes them balance: closing (`open` -> false) and unmounting while still
  // open both run the same cleanup exactly once. A leaked push here would leave
  // the app permanently "overlay open" and silently disable every shortcut.
  React.useEffect(() => {
    if (!open) return
    pushOverlay()
    return () => popOverlay()
  }, [open])

  // Position under the anchor (fixed, so it escapes overflow:hidden ancestors).
  // Measured (not just anchor-relative) so we can clamp fully on-screen and
  // flip above the anchor when there isn't room below — this runs once per
  // open, not on every scroll/resize; see the close-on-scroll effect below
  // for why live repositioning isn't needed.
  const [pos, setPos] = React.useState<{ top: number; left: number }>({ top: 0, left: 0 })
  React.useLayoutEffect(() => {
    if (!open) return
    const r = anchorRef.current?.getBoundingClientRect()
    if (!r) return
    const mw = listRef.current?.offsetWidth ?? 0
    const mh = listRef.current?.offsetHeight ?? 0
    const vw = window.innerWidth
    const vh = window.innerHeight
    const fitsBelow = r.bottom + GAP + mh <= vh
    const top = fitsBelow ? r.bottom + GAP : Math.max(GAP, r.top - GAP - mh)
    const rawLeft = align === 'end' ? r.right - mw : r.left
    const left = Math.min(Math.max(rawLeft, GAP), Math.max(GAP, vw - mw - GAP))
    setPos({ top, left })
    setActive(enabledIdx[0] ?? 0)
  }, [open, anchorRef, align, enabledIdx])

  React.useEffect(() => {
    if (!open) return
    listRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]')[active]?.focus()
  }, [open, active])

  // Close on outside pointer-down. Kept separate from focus restoration
  // (below) so a caller passing an inline `onClose` — which gets a fresh
  // identity on every parent re-render — never causes this effect's cleanup
  // to run a focus change; it only ever tears down and reinstalls the
  // listener.
  React.useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (listRef.current?.contains(e.target as Node) || anchorRef.current?.contains(e.target as Node)) return
      onClose()
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open, onClose, anchorRef])

  // Close (don't reposition) on scroll of the page or any scrolling
  // ancestor — capture phase catches scroll events from inner containers,
  // which don't bubble. This is the documented minimum bar: an anchored
  // menu that can't track live repositioning simply closes instead of
  // drifting from its anchor.
  //
  // ORDERING CONTRACT: this effect must stay declared *after* the
  // focus-on-open effect above. Effects of the same kind (passive
  // useEffect) run in declaration order within a commit, so on the render
  // that opens the menu the active item's `.focus()` call above runs
  // *before* this scroll listener is attached. If a scroll ever were
  // triggered by that focus (e.g. an ancestor needing to reveal the newly
  // focused item — `pos` is already viewport-clamped so this shouldn't
  // normally be needed, but nothing guarantees a future change won't), the
  // listener isn't live yet and can't self-close the menu the instant it
  // opens. Reordering these two effects would silently break that.
  React.useEffect(() => {
    if (!open) return
    const onScroll = () => onClose()
    window.addEventListener('scroll', onScroll, true)
    return () => window.removeEventListener('scroll', onScroll, true)
  }, [open, onClose])

  // Restore focus to the anchor strictly on the open:true -> false
  // transition — never as a side effect of an unrelated re-render (see the
  // outside-pointerdown effect's comment above for why that split matters).
  // `skipRestoreRef` lets the Tab handler opt out of this restore: Tab
  // closes without preventDefault so the browser can advance focus to the
  // next control natively, and if this effect unconditionally refocused the
  // anchor afterward it would override that native transfer, making Tab
  // behave like Escape instead of like Tab.
  const wasOpenRef = React.useRef(open)
  const skipRestoreRef = React.useRef(false)
  React.useEffect(() => {
    if (wasOpenRef.current && !open) {
      if (skipRestoreRef.current) skipRestoreRef.current = false
      else anchorRef.current?.focus()
    }
    wasOpenRef.current = open
  }, [open, anchorRef])

  if (!open) return null

  const pick = (i: number) => {
    const it = items[i]
    if (!it || it.disabled) return
    onSelect(it.id)
    onClose()
  }
  const move = (delta: number) => {
    if (enabledIdx.length === 0) return
    const k = enabledIdx.indexOf(active)
    const next = enabledIdx[(k + delta + enabledIdx.length) % enabledIdx.length]
    if (next !== undefined) setActive(next)
  }
  // Typeahead: search starting just *after* the current item and wrap
  // around, so repeated presses of the same letter cycle through every
  // enabled item sharing that initial rather than always landing on the
  // first one.
  const typeahead = (ch: string) => {
    const n = enabledIdx.length
    if (n === 0) return
    const k = enabledIdx.indexOf(active)
    for (let step = 1; step <= n; step++) {
      const i = enabledIdx[(k + step + n) % n]
      if (i !== undefined && String(items[i].label).toLowerCase().startsWith(ch)) { setActive(i); return }
    }
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return }
    if (e.key === 'Home') { e.preventDefault(); setActive(enabledIdx[0] ?? 0); return }
    if (e.key === 'End') { e.preventDefault(); setActive(enabledIdx[enabledIdx.length - 1] ?? 0); return }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(active); return }
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return }
    // Tab is the standard ARIA-menu escape hatch: close and let focus move
    // on naturally — do not preventDefault. Flag the restore effect to skip
    // its anchor-focus so it doesn't clobber wherever the browser's native
    // Tab default-action just sent focus.
    if (e.key === 'Tab') { skipRestoreRef.current = true; onClose(); return }
    if (/^[1-9]$/.test(e.key)) { e.preventDefault(); const i = enabledIdx[Number(e.key) - 1]; if (i !== undefined) pick(i); return }
    if (e.key.length === 1) { typeahead(e.key.toLowerCase()) }
  }

  return createPortal(
    <div
      ref={listRef}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={{ position: 'fixed', top: pos.top, left: pos.left }}
      className={cn('z-popover min-w-48 rounded-md border border-edge bg-surface p-1 shadow-lg', className)}
    >
      {items.map((it, i) => {
        // Badge number derives from the exact same enabledIdx array the digit
        // shortcuts use, so a disabled item (absent from enabledIdx) never
        // gets a badge and never consumes a digit.
        const enabledPos = enabledIdx.indexOf(i)
        const n = enabledPos >= 0 ? enabledPos + 1 : null
        // Single-choice pickers (current permission mode / model): `selected`
        // being defined at all marks this as a radio-style item, which is
        // the only role `aria-checked` is valid on — a plain `menuitem`
        // never carries it.
        const role = it.selected === undefined ? 'menuitem' : 'menuitemradio'
        return (
          <div
            key={it.id}
            role={role}
            tabIndex={i === active ? 0 : -1}
            aria-disabled={it.disabled || undefined}
            aria-checked={role === 'menuitemradio' ? it.selected : undefined}
            onClick={() => pick(i)}
            onMouseEnter={() => !it.disabled && setActive(i)}
            className={cn(
              'flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-ink cursor-default select-none min-h-target',
              'focus:outline-none focus:bg-surface-raised',
              it.disabled && 'opacity-50',
              it.selected && 'font-semibold',
            )}
          >
            {it.icon}
            <span className="flex-1">{it.label}</span>
            {it.shortcut && <Kbd keys={it.shortcut} />}
            {n !== null && n <= 9 && <span aria-hidden="true" className="text-2xs text-ink-faint tabular-nums">{n}</span>}
          </div>
        )
      })}
    </div>,
    getOverlayRoot(),
  )
}
