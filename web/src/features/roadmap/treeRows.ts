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
  | { kind: 'initiative'; key: string; depth: number; product: ProductNode; initiative: InitiativeNode; expanded: boolean; hasChildren: boolean }
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

/** True when no persona filter is active, or `row` is a capability row that
 *  declares `personaSlug` — false for every other row kind while a filter IS
 *  active, so a product/initiative/pseudo row never "vacuously" survives a
 *  persona filter the way `rowMatches('')` would with an empty query; it only
 *  shows via the existing "kept when a descendant matches" recursion below. */
function personaMatches(row: TreeRow, personaSlug: string | null): boolean {
  if (personaSlug === null) return true
  return row.kind === 'capability' && row.capability.personaSlugs.includes(personaSlug)
}

/**
 * Flatten one product. Returns rows in display order. With a query, a row is
 * kept when it or any descendant matches; collapse is ignored so a search can
 * never be hidden by an earlier fold.
 */
function flattenProduct(product: ProductNode, state: TreeState, query: string, personaSlug: string | null): TreeRow[] {
  const searching = query.trim() !== '' || personaSlug !== null
  const known = new Set<string>()
  const collect = (c: CapabilityNode) => { known.add(c.slug); c.children.forEach(collect) }
  product.capabilities.forEach(collect)

  // A phase nests under its program (`byParent`); everything else — a root
  // initiative, or a phase whose parent names an initiative the product does
  // not have — is bucketed by primary capability like before. Membership is
  // decided ONCE, up front, from the product's own initiative slugs: a
  // dangling `parentInitiative` never hides a node, it just makes it a root.
  const initiativeSlugs = new Set(product.initiatives.map((i) => i.slug))
  const isRoot = (i: InitiativeNode) => i.parentInitiative === null || !initiativeSlugs.has(i.parentInitiative)

  const byParent = new Map<string, InitiativeNode[]>()
  const byPrimary = new Map<string, InitiativeNode[]>()
  const orphans: InitiativeNode[] = []
  for (const i of product.initiatives) {
    if (!isRoot(i)) {
      const list = byParent.get(i.parentInitiative!) ?? []
      list.push(i)
      byParent.set(i.parentInitiative!, list)
      continue
    }
    if (i.primaryCapability && known.has(i.primaryCapability)) {
      const list = byPrimary.get(i.primaryCapability) ?? []
      list.push(i)
      byPrimary.set(i.primaryCapability, list)
    } else {
      orphans.push(i)
    }
  }

  // Recursive: a program's phases follow it at `depth + 1`, folding with the
  // program's own key. While searching, a program is kept when IT matches or
  // any phase does (the same "keep it when a descendant matched" rule a
  // capability row already follows) — never filtered a second time by the
  // caller, which is what used to strand a matching phase under a
  // non-matching program title.
  const initiativeRows = (list: InitiativeNode[], depth: number): TreeRow[] =>
    list.flatMap((initiative) => {
      const key = initiativeKey(initiative.slug)
      const phases = byParent.get(initiative.slug) ?? []
      const kids = initiativeRows(phases, depth + 1)
      // `kids` is already self-filtered by the recursive call above (each
      // returned row survived because IT or a descendant matched, or because
      // we are not searching at all) — re-checking `rowMatches` here was a
      // proven no-op.
      const shown = kids
      const self: TreeRow = {
        kind: 'initiative', key, depth, product, initiative,
        expanded: searching ? shown.length > 0 : !state.collapsed.has(key),
        hasChildren: searching ? shown.length > 0 : phases.length > 0,
      }
      // Persona filtering never touches this function directly — an
      // initiative carries no persona of its own. Whether it even gets
      // CALLED for a given branch is decided by the caller (`orphanRows`
      // below, and `capPersonaOk` in `capRows`), so once we're here this is a
      // plain text match, same as before the persona filter existed.
      if (searching) {
        return rowMatches(self, query) || shown.length > 0 ? [self, ...shown] : []
      }
      return self.expanded ? [self, ...kids] : [self]
    })

  const capRows = (c: CapabilityNode, depth: number): TreeRow[] => {
    const key = capabilityKey(product.slug, c.slug)
    const self: TreeRow = {
      kind: 'capability', key, depth, product, capability: c,
      expanded: false, hasChildren: false,
    }
    const textMatch = rowMatches(self, query)
    // A capability's OWN persona match — ANDed with `textMatch` for the
    // capability's own self-match below, and also used to gate whether its
    // primary initiatives are even eligible: an initiative carries no persona
    // of its own, so under an active filter it can only survive under a
    // capability that does (steiner385/viberpit#2446). Without this gate, an
    // initiative with an empty/non-matching text query would trivially
    // "self-match" (see `rowMatches`'s empty-query short-circuit) and drag in
    // every capability that merely has primary initiatives, regardless of
    // persona — see the "no match anywhere" test in treeRows.test.ts.
    const capPersonaOk = personaMatches(self, personaSlug)
    // `initiativeRows` already self-filters while searching (a program row is
    // kept for a matching phase even when its own title does not match), so —
    // same as the nested-capability branch just above it — nothing here
    // filters the result a second time.
    const kids: TreeRow[] = [
      ...c.children.flatMap((k) => capRows(k, depth + 1)),
      ...(capPersonaOk ? initiativeRows(byPrimary.get(c.slug) ?? [], depth + 1) : []),
    ]
    // While searching the fold is ignored — `kids` are emitted whatever the
    // collapse state — so the row reports what it ACTUALLY renders. Reporting
    // the stored fold instead would give a visibly expanded row an
    // `aria-expanded="false"` and an "Expand" chevron that does nothing.
    self.expanded = searching ? kids.length > 0 : !state.collapsed.has(key)
    self.hasChildren = searching
      ? kids.length > 0
      : c.children.length > 0 || (byPrimary.get(c.slug)?.length ?? 0) > 0
    if (searching) {
      return (textMatch && capPersonaOk) || kids.length > 0 ? [self, ...kids] : []
    }
    return self.expanded ? [self, ...kids] : [self]
  }

  // When a persona filter is active, don't include orphan initiatives or pseudo
  // rows, since they have no persona of their own and are gated only indirectly
  // through matching capabilities. Primary initiatives of matching capabilities
  // are already included via capRows.
  const orphanRows = personaSlug !== null ? [] : initiativeRows(orphans, 1)
  const pseudoRows = personaSlug !== null ? [] : [
    ...(product.counts.awaitingTriage > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'awaiting-triage'), depth: 1, product, pseudo: 'awaiting-triage', count: product.counts.awaitingTriage } as TreeRow]
      : []),
    ...(product.counts.needsHome > 0
      ? [{ kind: 'pseudo', key: pseudoKey(product.slug, 'needs-home'), depth: 1, product, pseudo: 'needs-home', count: product.counts.needsHome } as TreeRow]
      : []),
  ]

  const children: TreeRow[] = [
    ...product.capabilities.flatMap((c) => capRows(c, 1)),
    ...orphanRows,
    ...pseudoRows,
  ]
  const key = productKey(product.slug)
  // Pseudo rows were not filtered above; filter here. Capability AND initiative
  // rows are already self-filtered by their own recursive builders (a program
  // row is kept for a matching phase the same way a capability is kept for a
  // matching child), so re-checking `rowMatches` on them here would drop a
  // program whose own title does not match but whose phase does.
  const kept = searching
    ? children.filter((r) => r.kind === 'capability' || r.kind === 'initiative' || rowMatches(r, query))
    : children
  const self: TreeRow = {
    kind: 'product',
    key,
    depth: 0,
    product,
    // Same honesty rule as a capability row: under a query the fold is ignored,
    // so `expanded` describes the rendered subtree, not the stored fold.
    expanded: searching ? kept.length > 0 : !state.collapsed.has(key),
    hasChildren: searching ? kept.length > 0 : children.length > 0,
  }
  if (searching) {
    return (rowMatches(self, query) && personaMatches(self, personaSlug)) || kept.length > 0 ? [self, ...kept] : []
  }
  return self.expanded ? [self, ...children] : [self]
}

export function flattenMap(map: RoadmapMap, state: TreeState, query: string, personaSlug: string | null = null): TreeRow[] {
  return map.products.flatMap((p) => flattenProduct(p, state, query, personaSlug))
}

/** Every distinct persona slug declared anywhere in the map, sorted — the
 *  filter dropdown's option list (steiner385/viberpit#2446). Derived from the
 *  map already on hand rather than a second per-product fetch: a capability's
 *  resolved personaSlugs are already threaded onto every node (Task 1). */
export function personaSlugsInMap(map: RoadmapMap): string[] {
  const seen = new Set<string>()
  const walk = (c: CapabilityNode) => { c.personaSlugs.forEach((s) => seen.add(s)); c.children.forEach(walk) }
  map.products.forEach((p) => p.capabilities.forEach(walk))
  return [...seen].sort()
}
