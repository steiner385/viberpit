/**
 * Finding a node in the roadmap map by slug — pure, so the pages can look up
 * what the URL names without a second fetch (the map is one query, shared by
 * the list column and every detail level).
 */
import type { CapabilityNode, MapChangesPointer, ProductNode, RoadmapMap } from '@/api/roadmap'

export function findProduct(map: RoadmapMap | undefined, slug: string): ProductNode | null {
  return map?.products.find((p) => p.slug === slug) ?? null
}

/**
 * The product's map-changes pointer, but only while it is a LIVE PR: a merged
 * or abandoned pointer is history the map already reflects, and showing it would
 * offer Rebase and Abandon on a PR that is no longer there.
 *
 * One reading, here, because two surfaces answer "is there an open PR?" — the
 * product Overview's `MapChangesCard` and a capability's "Changes pending in
 * PR #N" note — and a status either surface forgot to check would put a dead PR
 * back on the page. NOT for the tree row's `PR #N` badge: that reads the
 * capability's OWN `prNumber`, which the sync clears when the PR merges, and is
 * a fact about the capability rather than about the product's pointer.
 */
export function openMapChanges(product: ProductNode | null | undefined): MapChangesPointer | null {
  const pointer = product?.mapChanges
  return pointer && pointer.status === 'open' ? pointer : null
}

export function findCapability(product: ProductNode, slug: string): CapabilityNode | null {
  const walk = (nodes: CapabilityNode[]): CapabilityNode | null => {
    for (const n of nodes) {
      if (n.slug === slug) return n
      const hit = walk(n.children)
      if (hit) return hit
    }
    return null
  }
  return walk(product.capabilities)
}

/**
 * The capability tree as an indented flat list, so a `select` can offer the
 * whole tree without pretending it is flat. Depth-first, parents before their
 * children, `depth` carrying the indent the option renders with.
 *
 * Here rather than in a dialog: both places that let a human pick a capability
 * — "+ Initiative" and the Inbox's proposal editor — flatten the same tree the
 * same way, and two copies would be two indent rules.
 */
export function flattenCapabilities(
  nodes: CapabilityNode[], depth = 0,
): { node: CapabilityNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flattenCapabilities(n.children, depth + 1)])
}

/** The slug path from the root to `slug`, for breadcrumbs. */
export function capabilityTrail(product: ProductNode, slug: string): CapabilityNode[] {
  const walk = (nodes: CapabilityNode[], trail: CapabilityNode[]): CapabilityNode[] | null => {
    for (const n of nodes) {
      const next = [...trail, n]
      if (n.slug === slug) return next
      const hit = walk(n.children, next)
      if (hit) return hit
    }
    return null
  }
  return walk(product.capabilities, []) ?? []
}
