/**
 * Where the roadmap lives and what is open — the one module that spells these
 * paths (finish-line spec §4.2), modelled on `session/sessionNavState.ts`.
 * The route PARAM NAMES are the contract: the open-item resolver
 * discriminates on them alone.
 */
import { capabilityKey, initiativeKey, productKey } from './treeRows'

export const ROADMAP_PATH = '/roadmap'
export const PRODUCT_ROUTE = `${ROADMAP_PATH}/:productSlug`
export const CAPABILITY_ROUTE = `${ROADMAP_PATH}/:productSlug/capabilities/:capSlug`
/** Initiative detail keeps its own prefix (roadmap spec §4): `/initiatives/:slug`. */
export const INITIATIVES_PATH = '/initiatives'
export const INITIATIVE_ROUTE = `${INITIATIVES_PATH}/:initiativeSlug`

export type RoadmapScope = 'map' | 'initiatives' | 'inbox'
export const ROADMAP_SCOPES: readonly RoadmapScope[] = ['map', 'initiatives', 'inbox']

export function roadmapScopeForSearch(search: string): RoadmapScope {
  const v = new URLSearchParams(search).get('scope')
  return (ROADMAP_SCOPES as readonly string[]).includes(v ?? '') ? (v as RoadmapScope) : 'map'
}

export function roadmapPath(scope: RoadmapScope = 'map'): string {
  return scope === 'map' ? ROADMAP_PATH : `${ROADMAP_PATH}?scope=${scope}`
}
export const productPath = (productSlug: string) =>
  `${ROADMAP_PATH}/${encodeURIComponent(productSlug)}`
export const capabilityPath = (productSlug: string, capSlug: string) =>
  `${productPath(productSlug)}/capabilities/${encodeURIComponent(capSlug)}`
export const initiativePath = (slug: string) => `${INITIATIVES_PATH}/${encodeURIComponent(slug)}`

/** The Inbox scope, optionally narrowed to one product and/or with one proposal open. */
export function inboxPath({ product, proposal }: { product?: string | null; proposal?: string | null } = {}): string {
  const parts = ['scope=inbox']
  if (product) parts.push(`product=${encodeURIComponent(product)}`)
  if (proposal) parts.push(`proposal=${encodeURIComponent(proposal)}`)
  return `${ROADMAP_PATH}?${parts.join('&')}`
}

export function inboxProductForSearch(search: string): string | null {
  const q = new URLSearchParams(search)
  return q.get('scope') === 'inbox' ? (q.get('product') || null) : null
}

export type RoadmapOpenItem =
  | { kind: 'product'; productSlug: string }
  | { kind: 'capability'; productSlug: string; capSlug: string }
  | { kind: 'initiative'; initiativeSlug: string }
  | { kind: 'proposal'; proposalId: string }

export function roadmapOpenItem(
  params: Readonly<Record<string, string | undefined>>,
  search: URLSearchParams,
): RoadmapOpenItem | null {
  const { productSlug, capSlug, initiativeSlug } = params
  if (initiativeSlug) return { kind: 'initiative', initiativeSlug }
  if (productSlug && capSlug) return { kind: 'capability', productSlug, capSlug }
  if (productSlug) return { kind: 'product', productSlug }
  const proposal = search.get('proposal')
  if (search.get('scope') === 'inbox' && proposal) return { kind: 'proposal', proposalId: proposal }
  return null
}

export const proposalKey = (id: string) => `proposal:${id}`

/** The list-row key an open item highlights. */
export function keyForOpenItem(item: RoadmapOpenItem | null): string | null {
  if (!item) return null
  if (item.kind === 'product') return productKey(item.productSlug)
  if (item.kind === 'capability') return capabilityKey(item.productSlug, item.capSlug)
  if (item.kind === 'proposal') return proposalKey(item.proposalId)
  return initiativeKey(item.initiativeSlug)
}
