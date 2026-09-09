// Map | Initiatives | Inbox — the roadmap list's scope switch.
//
// Shaped exactly like the sessions `ScopeSegment`: links, no state,
// `aria-current` marks the scope the URL names, and the name/count split is
// `ui/badgedName` + `ui/Badge` so a screen reader hears "Map (2)", never "Map2".
// Tokens only — this file is under `src/features/`.
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { ROADMAP } from '@/features/products/vocab'
import { roadmapPath, type RoadmapScope } from './roadmapNavState'

export interface RoadmapScopeSegmentProps {
  scope: RoadmapScope
  productCount: number
  initiativeCount: number
  /** Pending triage proposals. Counted in EVERY scope — a queue you are not
   *  looking at is exactly the one worth a badge. */
  inboxCount: number
}

const item = cn(
  'flex-1 inline-flex min-h-target items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const here = 'bg-accent-wash text-accent'
const elsewhere = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

interface ScopeLinkProps {
  to: string
  /** `badgedName` and `Badge` both read 0 as "no count" — a zero scope is
   *  announced and drawn bare rather than as a counter that failed. */
  count: number
  label: string
  current: boolean
  /** Tone of the count pill while the scope is NOT the current one. The Inbox
   *  is the only scope whose count is a backlog rather than an inventory, so it
   *  is the only one that asks for attention from another scope. */
  awayTone?: 'neutral' | 'warning'
}

function ScopeLink({ to, label, count, current, awayTone = 'neutral' }: ScopeLinkProps) {
  return (
    <Link to={to} aria-current={current ? 'page' : undefined} className={cn(item, current ? here : elsewhere)}>
      <span aria-hidden="true">{label}</span>
      <span className="sr-only">{badgedName({ label, count })}</span>
      {count > 0 && <Badge tone={current ? 'accent' : awayTone} count={count} aria-hidden="true" />}
    </Link>
  )
}

export function RoadmapScopeSegment({
  scope, productCount, initiativeCount, inboxCount,
}: RoadmapScopeSegmentProps) {
  return (
    <nav aria-label="Roadmap scope" className="flex gap-1 rounded-md border border-edge bg-surface p-1">
      <ScopeLink to={roadmapPath('map')} label={ROADMAP.map} count={productCount} current={scope === 'map'} />
      <ScopeLink
        to={roadmapPath('initiatives')}
        label={ROADMAP.initiatives}
        count={initiativeCount}
        current={scope === 'initiatives'}
      />
      <ScopeLink
        to={roadmapPath('inbox')}
        label={ROADMAP.inbox}
        count={inboxCount}
        current={scope === 'inbox'}
        awayTone="warning"
      />
    </nav>
  )
}
