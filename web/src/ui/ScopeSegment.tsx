import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Badge } from './Badge'
import { badgedName } from './badgedName'

export interface ScopeSegmentItem {
  to: string
  label: string
  /** `badgedName` and `Badge` both read 0 as "no count" — a zero scope is
   *  announced and drawn bare rather than as a counter that failed. */
  count?: number
  /** An amber dot and "(needs attention)" in the name, for a scope whose
   *  signal is yes/no rather than a number (Capacity's binding constraint). */
  attention?: boolean
  current: boolean
  /** Tone of the count pill while the scope is NOT the current one. A scope
   *  whose count is a backlog rather than an inventory (the roadmap Inbox)
   *  asks for attention from another scope with `warning`. */
  awayTone?: 'neutral' | 'warning'
}

export interface ScopeSegmentProps {
  /** Accessible name of the navigation ("Session scope", "Delivery scope"). */
  label: string
  items: ScopeSegmentItem[]
  className?: string
}

const item = cn(
  // Sized to fit three labelled, badged scopes in the default list column
  // (296px): `flex-auto` so widths follow the labels rather than equal thirds,
  // `text-xs` labels, tight padding. Before this "Capacity" was cut to
  // "Capacit" on Delivery and "Archived" clipped on Sessions at that width.
  'inline-flex flex-auto min-h-target items-center justify-center gap-1 whitespace-nowrap rounded-md px-1.5 text-xs font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const here = 'bg-accent-wash text-accent'
const elsewhere = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

/**
 * A list column's scope switch (Active | Background | Archived, Map |
 * Initiatives | Inbox, Automations | CI health | Capacity). A NAVIGATION that
 * holds no state: each half is a link to the path that owns that scope and
 * "you are here" is `aria-current`, driven by the scope the route produced.
 * Scope is a route, never `role="tab"` (docs/PAGE-TABS.md §2). The name/count
 * split is `badgedName` + `Badge`, so a screen reader hears "Automations (3)",
 * never "Automations3".
 *
 * `flex-wrap` is the fallback for a column dragged narrower than the labels
 * need: the last scope drops to a second row rather than being clipped or
 * scrolled out of sight, and comes back up as the column widens.
 */
export function ScopeSegment({ label, items, className }: ScopeSegmentProps) {
  return (
    <nav aria-label={label} className={cn('flex flex-wrap gap-1 rounded-md border border-edge bg-surface p-1', className)}>
      {items.map((s) => (
        <Link key={s.to} to={s.to} aria-current={s.current ? 'page' : undefined} className={cn(item, s.current ? here : elsewhere)}>
          <span aria-hidden="true">{s.label}</span>
          <span className="sr-only">{badgedName({ label: s.label, count: s.count, attention: s.attention })}</span>
          {s.attention && <Badge tone="warning" dot />}
          {s.count != null && s.count > 0 && (
            <Badge tone={s.current ? 'accent' : s.awayTone ?? 'neutral'} count={s.count} aria-hidden="true" />
          )}
        </Link>
      ))}
    </nav>
  )
}
