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

export function Tabs({ items, value, onChange, label, panelId, orientation = 'horizontal', className }: TabsProps) {
  const refs = React.useRef<(HTMLButtonElement | null)[]>([])
  const horizontal = orientation === 'horizontal'
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
      role="tablist"
      aria-label={label}
      aria-orientation={orientation}
      className={cn(
        'flex gap-1',
        horizontal ? 'overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden' : 'flex-col',
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
