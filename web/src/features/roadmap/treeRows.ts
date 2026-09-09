/**
 * The Map scope's row model (roadmap spec §6): a pure flattening of the map
 * payload into the rows the list column renders, with collapse and search.
 * No React, no I/O — `RoadmapListColumn` calls it on every render.
 */
import type { CapabilityNode, InitiativeNode, ProductNode, RoadmapMap } from '@/api/roadmap'

export type PseudoKind = 'awaiting-triage' | 'needs-home'

export type TreeRow =
  | { kind: 'product'; key: string; depth: 0; product: ProductNode; expanded: boolean; hasChildren: boolean }
  | { kind: 'capability'; key: string; depth: number; product: ProductNode; capability: CapabilityNode; expanded: boolean; hasChildren: boolean }
  | { kind: 'initiative'; key: string; depth: number; product: ProductNode; initiative: InitiativeNode }
  | { kind: 'pseudo'; key: string; depth: 1; product: ProductNode; pseudo: PseudoKind; count: number }

export interface TreeState {
  /** Keys of COLLAPSED nodes. Everything is expanded by default so a new
   *  product is fully visible the first time it appears. */
  collapsed: ReadonlySet<string>
}

export const EMPTY_TREE_STATE: TreeState = { collapsed: new Set() }

export const productKey = (slug: string) => `product:${slug}`
export const capabilityKey = (productSlug: string, capSlug: string) => `capability:${productSlug}:${capSlug}`
export const initiativeKey = (slug: string) => `initiative:${slug}`
const pseudoKey = (productSlug: string, kind: PseudoKind) => `pseudo:${productSlug}:${kind}`

export function toggleKey(state: TreeState, key: string): TreeState {
  return setCollapsed(state, key, !state.collapsed.has(key))
}

export function setCollapsed(state: TreeState, key: string, collapsed: boolean): TreeState {
  if (state.collapsed.has(key) === collapsed) return state
  const next = new Set(state.collapsed)
  if (collapsed) next.add(key)
  else next.delete(key)
  return { collapsed: next }
}

function norm(s: string | null | undefined): string {
  return (s ?? '').toLowerCase()
}

/** Title or slug contains every whitespace-separated term of `query`. */
export function rowMatches(row: TreeRow, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  const hay = row.kind === 'product' ? `${norm(row.product.name)} ${norm(row.product.slug)}`
    : row.kind === 'capability' ? `${norm(row.capability.title)} ${norm(row.capability.slug)}`
    : row.kind === 'initiative' ? `${norm(row.initiative.title)} ${norm(row.initiative.slug)}`
    : row.pseudo
  return terms.every((t) => hay.includes(t))
}

/**
 * Flatten one product. Returns rows in display order. With a query, a row is
 * kept when it or any descendant matches; collapse is ignored so a search can
 * never be hidden by an earlier fold.
 */
function flattenProduct(product: ProductNode, state: TreeState, query: string): TreeRow[] {
  const searching = query.trim() !== ''
  const byPrimary = new Map<string, InitiativeNode[]>()
  const orphans: InitiativeNode[] = []
  const known = new Set<string>()
  const collect = (c: CapabilityNode) => { known.add(c.slug); c.children.forEach(collect) }
  product.capabilities.forEach(collect)
  for (const i of product.initiatives) {
    if (i.primaryCapability && known.has(i.primaryCapability)) {
      const list = byPrimary.get(i.primaryCapability) ?? []
      list.push(i)
      byPrimary.set(i.primaryCapability, list)
    } else {
      orphans.push(i)
    }
  }

  const initiativeRows = (list: InitiativeNode[], depth: number): TreeRow[] =>
    list.map((initiative) => ({ kind: 'initiative', key: initiativeKey(initiative.slug), depth, product, initiative }))

  const capRows = (c: CapabilityNode, depth: number): TreeRow[] => {
    const key = capabilityKey(product.slug, c.slug)
    const kids: TreeRow[] = searching
      ? [
          ...c.children.flatMap((k) => capRows(k, depth + 1)),
          ...initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1).filter((r) => rowMatches(r, query)),
        ]
      : [
          ...c.children.flatMap((k) => capRows(k, depth + 1)),
          ...initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1),
        ]
    const self: TreeRow = { kind: 'capability', key, depth, product, capability: c, expanded: !state.collapsed.has(key), hasChildren: c.children.length > 0 || (byPrimary.get(c.slug)?.length ?? 0) > 0 }
    if (searching) {
      return rowMatches(self, query) || kids.length > 0 ? [self, ...kids] : []
    }
    return self.expanded ? [self, ...kids] : [self]
  }

  const children: TreeRow[] = [
    ...product.capabilities.flatMap((c) => capRows(c, 1)),
    ...initiativeRows(orphans, 1),
    ...(product.counts.awaitingTriage > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'awaiting-triage'), depth: 1, product, pseudo: 'awaiting-triage', count: product.counts.awaitingTriage } as TreeRow]
      : []),
    ...(product.counts.needsHome > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'needs-home'), depth: 1, product, pseudo: 'needs-home', count: product.counts.needsHome } as TreeRow]
      : []),
  ]
  const key = productKey(product.slug)
  const self: TreeRow = { kind: 'product', key, depth: 0, product, expanded: !state.collapsed.has(key), hasChildren: children.length > 0 }
  if (searching) {
    // Leaf rows (initiatives, pseudo) were not filtered by capRows; filter here.
    const kept = children.filter((r) => r.kind === 'capability' || rowMatches(r, query))
    return rowMatches(self, query) || kept.length > 0 ? [self, ...kept] : []
  }
  return self.expanded ? [self, ...children] : [self]
}

export function flattenMap(map: RoadmapMap, state: TreeState, query: string): TreeRow[] {
  return map.products.flatMap((p) => flattenProduct(p, state, query))
}
