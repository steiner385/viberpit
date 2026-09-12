// What "needs you" means, in one place: the rows `NeedsYouList` renders and the
// number a Needs-you badge shows. Its own module so the badge on a detail header
// and the list in the panel below it cannot drift apart (and so `NeedsYouList`
// stays a component-only file for fast refresh).
import type { InitiativeNode, ProductNode, RoadmapTier } from '@/api/roadmap'

export interface NeedsYouRows {
  /** Initiatives the server tiered `needs-you` or `blocked`, each with the product it is in. */
  initiatives: { product: ProductNode; initiative: InitiativeNode }[]
  /** One row per product that has proposals awaiting a human decision. */
  pseudo: ProductNode[]
}

/** Needs-you first (a decision), then blocked (a stall a human can clear) —
 *  the design spec's item 17 puts both in this pile, in tier order. */
const NEEDS_YOU_TIERS: readonly RoadmapTier[] = ['needs-you', 'blocked']

export function needsYouRows(products: readonly ProductNode[]): NeedsYouRows {
  return {
    initiatives: NEEDS_YOU_TIERS.flatMap((tier) =>
      products.flatMap((p) =>
        p.initiatives.filter((i) => i.tier === tier).map((i) => ({ product: p, initiative: i })))),
    pseudo: products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0),
  }
}

/** The number a "Needs you" badge shows: the rows the list renders that the map
 *  can count. Incidents are deliberately excluded — they come from a different
 *  query, and `IncidentQueue` badges its own live count in its own header. */
export function needsYouCount(products: readonly ProductNode[]): number {
  const { initiatives, pseudo } = needsYouRows(products)
  return initiatives.length + pseudo.length
}
