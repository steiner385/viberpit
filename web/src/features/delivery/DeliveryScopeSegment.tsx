// Automations | CI health | Capacity — the Delivery list's scope switch, on
// the `ui/ScopeSegment` primitive (shared with Sessions and Roadmap).
import { ScopeSegment } from '@/ui/ScopeSegment'
import { DELIVERY } from '@/features/products/vocab'
import { deliveryPath, type DeliveryScope } from './deliveryNavState'

export interface DeliveryScopeSegmentProps {
  scope: DeliveryScope
  automationCount: number
  ciCount: number
  /** 1 when the readout has a binding constraint, else 0 — Capacity has no
   *  actionable-item count of its own, just whether the fleet is blocked, so
   *  it is drawn as an attention dot, not as a count of one. */
  capacityCount: number
}

export function DeliveryScopeSegment({ scope, automationCount, ciCount, capacityCount }: DeliveryScopeSegmentProps) {
  return (
    <ScopeSegment
      label="Delivery scope"
      items={[
        { to: deliveryPath('automations'), label: DELIVERY.automations, count: automationCount, current: scope === 'automations' },
        { to: deliveryPath('ci'), label: DELIVERY.ci, count: ciCount, current: scope === 'ci' },
        { to: deliveryPath('capacity'), label: DELIVERY.capacity, attention: capacityCount > 0, current: scope === 'capacity' },
      ]}
    />
  )
}
