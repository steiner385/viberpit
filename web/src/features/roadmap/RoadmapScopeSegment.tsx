// Map | Initiatives | Inbox — the roadmap list's scope switch, on the
// `ui/ScopeSegment` primitive (shared with Sessions and Delivery).
import { ScopeSegment } from '@/ui/ScopeSegment'
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

export function RoadmapScopeSegment({
  scope, productCount, initiativeCount, inboxCount,
}: RoadmapScopeSegmentProps) {
  return (
    <ScopeSegment
      label="Roadmap scope"
      items={[
        { to: roadmapPath('map'), label: ROADMAP.map, count: productCount, current: scope === 'map' },
        { to: roadmapPath('initiatives'), label: ROADMAP.initiatives, count: initiativeCount, current: scope === 'initiatives' },
        // The Inbox is the only scope whose count is a backlog rather than an
        // inventory, so it is the only one that asks for attention from another scope.
        { to: roadmapPath('inbox'), label: ROADMAP.inbox, count: inboxCount, current: scope === 'inbox', awayTone: 'warning' },
      ]}
    />
  )
}
