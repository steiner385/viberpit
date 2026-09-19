// Automations | CI health — the Delivery list's scope switch. Copy of
// `features/roadmap/RoadmapScopeSegment.tsx` with two links instead of three:
// same link/badge/aria-current shape so a screen reader hears "Automations
// (3)", never "Automations3".
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { DELIVERY } from '@/features/products/vocab'
import { deliveryPath, type DeliveryScope } from './deliveryNavState'

export interface DeliveryScopeSegmentProps {
  scope: DeliveryScope
  automationCount: number
  ciCount: number
  /** 1 when the readout has a binding constraint, else 0 — Capacity has no
   *  actionable-item count of its own, just whether the fleet is blocked. */
  capacityCount: number
}

const item = cn(
  'flex-1 inline-flex min-h-target items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const here = 'bg-accent-wash text-accent'
const elsewhere = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

interface ScopeLinkProps {
  to: string
  count: number
  label: string
  current: boolean
}

function ScopeLink({ to, label, count, current }: ScopeLinkProps) {
  return (
    <Link to={to} aria-current={current ? 'page' : undefined} className={cn(item, current ? here : elsewhere)}>
      <span aria-hidden="true">{label}</span>
      <span className="sr-only">{badgedName({ label, count })}</span>
      {count > 0 && <Badge tone={current ? 'accent' : 'neutral'} count={count} aria-hidden="true" />}
    </Link>
  )
}

export function DeliveryScopeSegment({ scope, automationCount, ciCount, capacityCount }: DeliveryScopeSegmentProps) {
  return (
    <nav aria-label="Delivery scope" className="flex gap-1 rounded-md border border-edge bg-surface p-1">
      <ScopeLink
        to={deliveryPath('automations')}
        label={DELIVERY.automations}
        count={automationCount}
        current={scope === 'automations'}
      />
      <ScopeLink to={deliveryPath('ci')} label={DELIVERY.ci} count={ciCount} current={scope === 'ci'} />
      <ScopeLink
        to={deliveryPath('capacity')}
        label={DELIVERY.capacity}
        count={capacityCount}
        current={scope === 'capacity'}
      />
    </nav>
  )
}
