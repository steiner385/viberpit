/**
 * Finding a node in the roadmap map by slug — pure, so the pages can look up
 * what the URL names without a second fetch (the map is one query, shared by
 * the list column and every detail level).
 */
import type { CapabilityNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export function findProduct(map: RoadmapMap | undefined, slug: string): ProductNode | null {
  return map?.products.find((p) => p.slug === slug) ?? null
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
