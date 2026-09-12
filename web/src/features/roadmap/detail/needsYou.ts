// What "needs you" means, in one place: the rows `NeedsYouList` renders and the
// number a Needs-you badge shows. Its own module so the badge on a detail header
// and the list in the panel below it cannot drift apart (and so `NeedsYouList`
// stays a component-only file for fast refresh).
import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapTier } from '@/api/roadmap'
import { flattenCapabilities } from '../lookup'

export interface NeedsYouRows {
  /** Direct attention issues grouped by capability, without descendant rollup duplicates. */
  issueGroups: { product: ProductNode; capability: CapabilityNode; count: number }[]
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
    issueGroups: products.flatMap((product) => {
      // Capability counts include primary non-program initiatives as well as
      // issues. Index them once so each capability need not rescan the product.
      const initiativeCounts = new Map<string, { needsYou: number; blocked: number }>()
      for (const initiative of product.initiatives) {
        if (!initiative.primaryCapability || initiative.hasChildren) continue
        const counts = initiativeCounts.get(initiative.primaryCapability) ?? { needsYou: 0, blocked: 0 }
        if (initiative.tier === 'needs-you') counts.needsYou += 1
        if (initiative.tier === 'blocked') counts.blocked += 1
        initiativeCounts.set(initiative.primaryCapability, counts)
      }
      return flattenCapabilities(product.capabilities).flatMap(({ node: capability }) => {
        const primary = initiativeCounts.get(capability.slug)
        // Match server/pm/tree.py:_capability_node: subtract child rollups and
        // primary leaf initiatives separately for each attention tier.
        const count = (['needsYou', 'blocked'] as const).reduce((total, tier) => {
          const childCount = capability.children.reduce((n, child) => n + child.counts[tier], 0)
          return total + Math.max(0, capability.counts[tier] - childCount - (primary?.[tier] ?? 0))
        }, 0)
        return count > 0 ? [{ product, capability, count }] : []
      })
    }),
    initiatives: NEEDS_YOU_TIERS.flatMap((tier) =>
      products.flatMap((p) =>
        p.initiatives.filter((i) => i.tier === tier).map((i) => ({ product: p, initiative: i })))),
    pseudo: products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0),
  }
}

/** The number a "Needs you" badge shows: individual attention issues plus
 *  initiative and proposal rows that the map can count. Incidents are deliberately excluded — they come from a different
 *  query, and `IncidentQueue` badges its own live count in its own header. */
export function needsYouCount(products: readonly ProductNode[]): number {
  const { initiatives, pseudo, issueGroups } = needsYouRows(products)
  return initiatives.length + pseudo.length + issueGroups.reduce((n, group) => n + group.count, 0)
}
