import * as React from 'react'
import { cn } from '@/lib/utils'
import { Badge } from './Badge'
import { badgedName } from './badgedName'

export interface TabItem { id: string; label: string; count?: number; attention?: boolean }

export interface TabsProps {
  items: TabItem[]
  value: string
  onChange: (id: string) => void
  /** Accessible name for the tablist. */
  label: string
  /** When provided, each tab gets id="tab-<id>" and aria-controls={panelId(id)}. */
  panelId?: (id: string) => string
  orientation?: 'horizontal' | 'vertical'
  className?: string
}

/** Width of the edge fade that says "more tabs this way", and the room kept
 *  between the selected tab and that edge so the fade never sits on it. */
const EDGE = 24

/** `mask-image` fades for a strip with tabs hidden past one or both edges. A
 *  mask rather than an overlay, so the cue works on whatever the strip sits
 *  on — the page canvas under a detail pane, `bg-canvas` in the session body. */
const FADE = {
  left: '[mask-image:linear-gradient(to_right,transparent,black_1.5rem)]',
  right: '[mask-image:linear-gradient(to_right,black_calc(100%-1.5rem),transparent)]',
  both: '[mask-image:linear-gradient(to_right,transparent,black_1.5rem,black_calc(100%-1.5rem),transparent)]',
} as const

export function Tabs({ items, value, onChange, label, panelId, orientation = 'horizontal', className }: TabsProps) {
  const refs = React.useRef<(HTMLButtonElement | null)[]>([])
  const listRef = React.useRef<HTMLDivElement | null>(null)
  const horizontal = orientation === 'horizontal'
  // Which edges currently hide tabs. A six-tab strip keeps three tabs visible
  // at 390px (docs/PAGE-TABS.md §4) and the scrollbar is hidden, so without
  // this the other three simply do not exist to the reader.
  const [hidden, setHidden] = React.useState({ left: false, right: false })

  const measure = React.useCallback(() => {
    const el = listRef.current
    if (!el) return
    const left = el.scrollLeft > 1
    const right = el.scrollWidth - el.clientWidth - el.scrollLeft > 1
    setHidden((prev) => (prev.left === left && prev.right === right ? prev : { left, right }))
  }, [])

  // Re-measure when the box changes size (a rotated phone, a resized list
  // column). jsdom has neither layout nor ResizeObserver, so both are guarded.
  React.useEffect(() => {
    if (!horizontal) return
    measure()
    const el = listRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [horizontal, measure, items.length])

  // Keep the selected tab on screen: a deep link (`?tab=sessions`) on a phone
  // must not land with its own tab scrolled out of the strip. Keyed on the
  // value and the selected tab's own badge (a count that arrives after mount
  // widens the tab past the edge it was just scrolled to), not on the items
  // array (rebuilt every render), so a poll-driven re-render never yanks a
  // strip the reader has scrolled by hand.
  const activeCount = items.find((t) => t.id === value)?.count
  React.useEffect(() => {
    if (!horizontal) return
    const list = listRef.current
    const tab = refs.current[items.findIndex((t) => t.id === value)]
    if (!list || !tab) return
    const start = tab.offsetLeft - EDGE
    const end = tab.offsetLeft + tab.offsetWidth + EDGE
    if (start < list.scrollLeft) list.scrollLeft = Math.max(0, start)
    else if (end > list.scrollLeft + list.clientWidth) list.scrollLeft = end - list.clientWidth
    measure()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see the comment above: `items` is intentionally not a dependency
  }, [horizontal, value, items.length, activeCount, measure])

  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    const next = e.key === (horizontal ? 'ArrowRight' : 'ArrowDown') ? (i + 1) % items.length
      : e.key === (horizontal ? 'ArrowLeft' : 'ArrowUp') ? (i - 1 + items.length) % items.length
      : e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : -1
    if (next < 0) return
    e.preventDefault()
    refs.current[next]?.focus()
  }
  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={label}
      aria-orientation={orientation}
      onScroll={horizontal ? measure : undefined}
      data-hidden-left={hidden.left || undefined}
      data-hidden-right={hidden.right || undefined}
      className={cn(
        // `shrink-0`: a horizontal strip is `overflow-x-auto`, which makes it a
        // scroll container whose automatic min-height is ZERO — so inside a
        // `min-h-0` flex column (every `PageContent`) a tall panel below it
        // squeezed the strip to its 1px border. Measured on 9 of 11 Delivery
        // detail routes on desktop and every one on a phone (2026-10-01
        // Delivery critique, P0). The strip owns the rule so no caller can
        // reproduce it.
        'flex shrink-0 gap-1',
        // `relative` so a tab's offsetLeft is measured from the strip. The
        // baseline rule belongs to the strip, not its callers: the tabs'
        // `-mb-px` exists to sit their 2px indicator on it.
        horizontal
          ? 'relative overflow-x-auto border-b border-edge [scrollbar-width:none] [&::-webkit-scrollbar]:hidden'
          : 'flex-col',
        horizontal && hidden.left && hidden.right && FADE.both,
        horizontal && hidden.left && !hidden.right && FADE.left,
        horizontal && !hidden.left && hidden.right && FADE.right,
        className,
      )}
    >
      {items.map((t, i) => {
        const active = t.id === value
        return (
          <button
            key={t.id}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="tab"
            id={panelId ? `tab-${t.id}` : undefined}
            aria-selected={active}
            aria-controls={panelId ? panelId(t.id) : undefined}
            aria-label={badgedName(t)}
            tabIndex={active ? 0 : -1}
            onKeyDown={(e) => onKeyDown(e, i)}
            onClick={() => onChange(t.id)}
            className={cn(
              'shrink-0 whitespace-nowrap min-h-target px-3 py-2 text-sm font-medium flex items-center gap-1.5 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset',
              horizontal ? 'border-b-2 -mb-px' : 'border-l-2 rounded-r-md text-left',
              active ? 'border-accent text-accent' : 'border-transparent text-ink-muted hover:text-ink',
            )}
          >
            {t.label}
            {t.attention && <Badge tone="warning" dot />}
            {t.count != null && t.count > 0 && <Badge tone="neutral" count={t.count} />}
          </button>
        )
      })}
    </div>
  )
}
