/**
 * TabBar — the single source of truth for the app's section tabs.
 *
 * Previously the tab list, labels, active styling, the "Archived 111" count
 * badge, and the amber needs-attention dot were duplicated between the mobile
 * `Dashboard` and the wide-screen `DesktopShell`. This component owns all of it:
 * one canonical tab order, WAI-ARIA `role="tablist"` semantics with roving
 * `tabIndex` + arrow-key focus, count badges, and horizontal-scroll overflow so
 * every tab stays reachable on a narrow viewport.
 *
 * Callers supply a `TabDef[]` (label + optional count + optional attention flag)
 * and own the active state. Pass `panelId` to wire `aria-controls` to real
 * `role="tabpanel"` regions (the mobile Dashboard does; the desktop master-detail
 * pane is not a tabpanel, so it omits it).
 */
import * as React from 'react'
import { useRef } from 'react'
import { cn } from '@/lib/utils'
import type { TabId } from './Dashboard'
import type { TabDef } from './tabDefs'

export interface TabBarProps {
  tabs: TabDef[]
  activeTab: TabId
  onSelect: (id: TabId) => void
  /** Accessible label for the tablist container. */
  label: string
  /** When provided, each tab gets `id="tab-<id>"` and `aria-controls={panelId(id)}`. */
  panelId?: (id: TabId) => string
  /** Extra classes for the tablist container (e.g. a baseline border on mobile). */
  className?: string
}

/** Accessible name for a tab: label + count + attention, matching the old aria-labels. */
function tabAriaLabel({ label, count, attention }: TabDef): string {
  if (attention) return count ? `${label} (${count}, needs attention)` : `${label} (needs attention)`
  return count ? `${label} (${count})` : label
}

export function TabBar({ tabs, activeTab, onSelect, label, panelId, className }: TabBarProps) {
  const btnRefs = useRef<(HTMLButtonElement | null)[]>([])

  function handleKeyDown(e: React.KeyboardEvent, index: number) {
    let next: number
    if (e.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    else return
    e.preventDefault()
    btnRefs.current[next]?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      // `overflow-x-auto` keeps all tabs swipe-reachable on narrow viewports; the
      // scrollbar is hidden so the strip reads as a clean tab row, not a scroller.
      className={cn(
        'flex gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
    >
      {tabs.map((t, i) => (
        <button
          key={t.id}
          ref={(el) => { btnRefs.current[i] = el }}
          role="tab"
          id={panelId ? `tab-${t.id}` : undefined}
          aria-selected={activeTab === t.id}
          aria-controls={panelId ? panelId(t.id) : undefined}
          aria-label={tabAriaLabel(t)}
          // Roving tabindex: only the active tab is in the tab sequence; arrows move within.
          tabIndex={activeTab === t.id ? 0 : -1}
          onKeyDown={(e) => handleKeyDown(e, i)}
          onClick={() => onSelect(t.id)}
          className={cn(
            'shrink-0 whitespace-nowrap min-h-[44px] px-3 py-2 text-sm font-medium',
            'border-b-2 -mb-px transition-colors flex items-center gap-1.5',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset',
            activeTab === t.id
              ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
          )}
        >
          {t.label}
          {t.attention && (
            <span aria-hidden="true" className="inline-block w-2 h-2 rounded-full bg-amber-500 dark:bg-amber-400" />
          )}
          {t.count != null && t.count > 0 && (
            <span className="text-xs tabular-nums bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 rounded-full px-1.5 py-0.5 leading-none">
              {t.count}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
