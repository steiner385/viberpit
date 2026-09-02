import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
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

export function Menu({ open, onClose, anchorRef, items, onSelect, label, align = 'start', className }: MenuProps) {
  const listRef = React.useRef<HTMLDivElement>(null)
  const [active, setActive] = React.useState(0)
  // Single source of truth for "which items are selectable, in order": both
  // the digit shortcuts (enabledIdx[N-1]) and the rendered badge numbers
  // (enabledIdx.indexOf(i) + 1, below) index off this same array, so they
  // cannot disagree about which item is "Nth".
  const enabledIdx = React.useMemo(() => items.map((it, i) => (it.disabled ? -1 : i)).filter((i) => i >= 0), [items])

  // Position under the anchor (fixed, so it escapes overflow:hidden ancestors).
  const [pos, setPos] = React.useState<{ top: number; left: number; right?: number }>({ top: 0, left: 0 })
  React.useLayoutEffect(() => {
    if (!open) return
    const r = anchorRef.current?.getBoundingClientRect()
    if (!r) return
    setPos(align === 'end' ? { top: r.bottom + 4, left: 0, right: window.innerWidth - r.right } : { top: r.bottom + 4, left: r.left })
    setActive(enabledIdx[0] ?? 0)
  }, [open, anchorRef, align, enabledIdx])

  React.useEffect(() => {
    if (!open) return
    listRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]')[active]?.focus()
  }, [open, active])

  // Close on outside pointer-down; restore focus to the anchor on close.
  React.useEffect(() => {
    if (!open) return
    const anchor = anchorRef.current
    const onDown = (e: PointerEvent) => {
      if (listRef.current?.contains(e.target as Node) || anchor?.contains(e.target as Node)) return
      onClose()
    }
    document.addEventListener('pointerdown', onDown)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      anchor?.focus()
    }
  }, [open, onClose, anchorRef])

  if (!open) return null

  const pick = (i: number) => {
    const it = items[i]
    if (!it || it.disabled) return
    onSelect(it.id)
    onClose()
  }
  const move = (delta: number) => {
    const k = enabledIdx.indexOf(active)
    const next = enabledIdx[(k + delta + enabledIdx.length) % enabledIdx.length]
    if (next !== undefined) setActive(next)
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return }
    if (e.key === 'Home') { e.preventDefault(); setActive(enabledIdx[0] ?? 0); return }
    if (e.key === 'End') { e.preventDefault(); setActive(enabledIdx[enabledIdx.length - 1] ?? 0); return }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(active); return }
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return }
    if (/^[1-9]$/.test(e.key)) { e.preventDefault(); const i = enabledIdx[Number(e.key) - 1]; if (i !== undefined) pick(i); return }
    if (e.key.length === 1) {
      const ch = e.key.toLowerCase()
      const hit = enabledIdx.find((i) => String(items[i].label).toLowerCase().startsWith(ch))
      if (hit !== undefined) setActive(hit)
    }
  }

  return createPortal(
    <div
      ref={listRef}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={{ position: 'fixed', top: pos.top, left: pos.right === undefined ? pos.left : undefined, right: pos.right }}
      className={cn('z-popover min-w-48 rounded-md border border-edge bg-surface p-1 shadow-lg', className)}
    >
      {items.map((it, i) => {
        // Badge number derives from the exact same enabledIdx array the digit
        // shortcuts use, so a disabled item (absent from enabledIdx) never
        // gets a badge and never consumes a digit.
        const enabledPos = enabledIdx.indexOf(i)
        const n = enabledPos >= 0 ? enabledPos + 1 : null
        return (
          <div
            key={it.id}
            role="menuitem"
            tabIndex={i === active ? 0 : -1}
            aria-disabled={it.disabled || undefined}
            aria-checked={it.selected}
            onClick={() => pick(i)}
            onMouseEnter={() => !it.disabled && setActive(i)}
            className={cn(
              'flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-ink cursor-default select-none min-h-9',
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
