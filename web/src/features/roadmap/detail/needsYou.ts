// Canonical population for the attention list, overview and badge (design
// 2026-10-14 §1, `server/pm/attention.py`): an item NEEDS YOU only when a human
// alone can unblock it — the `needs-you` tier. Everything else that is stopped
// (the `blocked` tier) is WAITING: listed apart in `waitingRows`, never counted.
// Counts are individual attention issues, initiative decisions, and product
// triage rows.
import { WAITING_KEYS, type CapabilityNode, type InitiativeNode, type ProductNode, type RoadmapTier, type WaitingKey } from '@/api/roadmap'
import { WAITING_LABEL } from '@/features/products/vocab'
import { flattenCapabilities } from '../lookup'

type InitiativeRow = { product: ProductNode; initiative: InitiativeNode }

export interface NeedsYouRows {
  initiatives: InitiativeRow[]
  issues: { product: ProductNode; capability: CapabilityNode; needsYou: number }[]
  pseudo: ProductNode[]
}

export interface WaitingRows {
  initiatives: InitiativeRow[]
  issues: { product: ProductNode; capability: CapabilityNode; blocked: number }[]
}

/**
 * Newest first (spec §7).
 *
 * ISO strings compared as strings — the same idiom as the Inbox's `newest` sort
 * (`inbox/inboxOrdering.ts`), and correct because the server emits UTC with a
 * fixed offset. A node with no `updatedAt` (an older server, a hand-built
 * fixture) sorts last rather than first: `''` loses every comparison, so an
 * absent timestamp never jumps the queue. The slug tiebreak keeps the order
 * total, so a re-render of the same data never reshuffles the list.
 */
const newestFirst = (a: InitiativeNode, b: InitiativeNode) =>
  (b.updatedAt ?? '').localeCompare(a.updatedAt ?? '') || a.slug.localeCompare(b.slug)

/** The initiative's OWN tier: a program is raised to the max over its phases,
 *  and counting that would count it once more for every phase that needs you. */
const ownTier = (i: InitiativeNode): RoadmapTier => i.ownTier ?? i.tier

function scopedCapabilities(scope?: CapabilityNode): CapabilityNode[] | null {
  return scope ? flattenCapabilities([scope]).map(({ node }) => node) : null
}

function initiativesAt(products: readonly ProductNode[], tier: RoadmapTier, scopedCaps: CapabilityNode[] | null): InitiativeRow[] {
  return products.flatMap((product) =>
    product.initiatives.filter((i) => ownTier(i) === tier && (!scopedCaps || scopedCaps.some((c) =>
      i.primaryCapability === c.slug || c.initiatives.includes(i.slug),
    ))).map((initiative) => ({ product, initiative })))
    // Sorted after every product has contributed: a per-product sort would
    // interleave by product first and answer "newest in KinDash, then newest in
    // ViberPit", which is not one queue.
    .sort((x, y) => newestFirst(x.initiative, y.initiative))
}

/** Each capability's DIRECT issues in *tier*: server counts include child
 *  rollups AND primary leaf initiatives, so both are subtracted. Touching
 *  initiatives and programs contribute no count here. */
function directIssues(products: readonly ProductNode[], key: 'needsYou' | 'blocked', tier: RoadmapTier,
  scopedCaps: CapabilityNode[] | null): { product: ProductNode; capability: CapabilityNode; n: number }[] {
  return products.flatMap((product) =>
    (scopedCaps ?? flattenCapabilities(product.capabilities).map(({ node }) => node)).flatMap((capability) => {
      const primary = product.initiatives.filter((i) => i.primaryCapability === capability.slug && !i.hasChildren)
      const n = Math.max(0,
        capability.counts[key] - capability.children.reduce((sum, child) => sum + child.counts[key], 0)
        - primary.filter((i) => i.tier === tier).length)
      return n > 0 ? [{ product, capability, n }] : []
    }))
}

export function needsYouRows(products: readonly ProductNode[], scope?: CapabilityNode): NeedsYouRows {
  const scopedCaps = scopedCapabilities(scope)
  return {
    initiatives: initiativesAt(products, 'needs-you', scopedCaps),
    issues: directIssues(products, 'needsYou', 'needs-you', scopedCaps)
      .map(({ product, capability, n }) => ({ product, capability, needsYou: n })),
    pseudo: scope ? [] : products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0),
  }
}

/** Stopped, but on other work rather than on you: listed, never counted. */
export function waitingRows(products: readonly ProductNode[], scope?: CapabilityNode): WaitingRows {
  const scopedCaps = scopedCapabilities(scope)
  return {
    initiatives: initiativesAt(products, 'blocked', scopedCaps),
    issues: directIssues(products, 'blocked', 'blocked', scopedCaps)
      .map(({ product, capability, n }) => ({ product, capability, blocked: n })),
  }
}

/** Incidents have their own live count and query, and are excluded here. */
export function needsYouCount(products: readonly ProductNode[], scope?: CapabilityNode): number {
  const { initiatives, issues, pseudo } = needsYouRows(products, scope)
  return initiatives.length + issues.reduce((n, issue) => n + issue.needsYou, 0) + pseudo.length
}

/** The product's waiting counters (`counts.waiting`), non-zero only, in
 *  `WAITING_KEYS` order, with their words. Empty for an older server. */
export function waitingSummary(product: ProductNode): { key: WaitingKey; label: string; count: number }[] {
  const waiting = product.counts.waiting
  if (!waiting) return []
  return WAITING_KEYS.filter((key) => (waiting[key] ?? 0) > 0)
    .map((key) => ({ key, label: WAITING_LABEL[key], count: waiting[key] }))
}
