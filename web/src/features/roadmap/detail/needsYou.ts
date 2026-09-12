// Canonical population for the attention list, overview and badge. Counts are
// individual attention issues, initiative decisions, and product triage rows.
import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapTier } from '@/api/roadmap'
import { flattenCapabilities } from '../lookup'

export interface NeedsYouRows {
  initiatives: { product: ProductNode; initiative: InitiativeNode }[]
  issues: { product: ProductNode; capability: CapabilityNode; needsYou: number; blocked: number }[]
  pseudo: ProductNode[]
}

const NEEDS_YOU_TIERS: readonly RoadmapTier[] = ['needs-you', 'blocked']

export function needsYouRows(products: readonly ProductNode[], scope?: CapabilityNode): NeedsYouRows {
  const scopedCaps = scope ? flattenCapabilities([scope]).map(({ node }) => node) : null
  return {
    initiatives: NEEDS_YOU_TIERS.flatMap((tier) => products.flatMap((product) =>
      product.initiatives.filter((i) => i.tier === tier && (!scopedCaps || scopedCaps.some((c) =>
        i.primaryCapability === c.slug || c.initiatives.includes(i.slug),
      ))).map((initiative) => ({ product, initiative })))),
    issues: products.flatMap((product) =>
      (scopedCaps ?? flattenCapabilities(product.capabilities).map(({ node }) => node)).flatMap((capability) => {
        // Server counts include child rollups AND primary leaf initiatives.
        // Touching initiatives and programs contribute no count here.
        const primary = product.initiatives.filter((i) => i.primaryCapability === capability.slug && !i.hasChildren)
        const direct = (key: 'needsYou' | 'blocked', tier: RoadmapTier) => Math.max(0,
          capability.counts[key] - capability.children.reduce((n, child) => n + child.counts[key], 0)
          - primary.filter((i) => i.tier === tier).length)
        const needsYou = direct('needsYou', 'needs-you')
        const blocked = direct('blocked', 'blocked')
        return needsYou + blocked > 0 ? [{ product, capability, needsYou, blocked }] : []
      })),
    pseudo: scope ? [] : products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0),
  }
}

/** Incidents have their own live count and query, and are excluded here. */
export function needsYouCount(products: readonly ProductNode[], scope?: CapabilityNode): number {
  const { initiatives, issues, pseudo } = needsYouRows(products, scope)
  return initiatives.length + issues.reduce((n, issue) => n + issue.needsYou + issue.blocked, 0) + pseudo.length
}
