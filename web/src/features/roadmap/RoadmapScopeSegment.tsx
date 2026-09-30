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
  /** How many items across the portfolio wait on a human (`needsYouCount`). Lights
   *  the Map pill's attention dot and joins its name ("Map (6, needs attention)"):
   *  on a phone `/roadmap` renders only the tree, and this dot is the one glance
   *  signal that the map holds a queue (2026-09-30 Roadmap critique, P1). The
   *  count on the pill stays the product inventory. */
  needsYouCount?: number
}

export function RoadmapScopeSegment({
  scope, productCount, initiativeCount, inboxCount, needsYouCount = 0,
}: RoadmapScopeSegmentProps) {
  return (
    <ScopeSegment
      label="Roadmap scope"
      items={[
        { to: roadmapPath('map'), label: ROADMAP.map, count: productCount, attention: needsYouCount > 0, current: scope === 'map' },
        { to: roadmapPath('initiatives'), label: ROADMAP.initiatives, count: initiativeCount, current: scope === 'initiatives' },
        // The Inbox is the only scope whose count is a backlog rather than an
        // inventory, so it is the only one that asks for attention from another scope.
        { to: roadmapPath('inbox'), label: ROADMAP.inbox, count: inboxCount, current: scope === 'inbox', awayTone: 'warning' },
      ]}
    />
  )
}
