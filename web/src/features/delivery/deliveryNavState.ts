/**
 * Where the Delivery section lives and what is open — the one module that
 * spells these paths (finish-line spec §4.2), modelled on
 * `features/roadmap/roadmapNavState.ts`. The route PARAM NAMES are the contract:
 * the open-item resolver discriminates on them alone (`automationId`,
 * `owner` + `repo`, `capacityPane`), never on `id`, so a Delivery route can
 * never be mistaken for a live session.
 */
export const DELIVERY_PATH = '/delivery'
export const AUTOMATION_ROUTE = `${DELIVERY_PATH}/automations/:automationId`
export const CI_REPO_ROUTE = `${DELIVERY_PATH}/ci/:owner/:repo`
/**
 * The Capacity pane as an OPEN ITEM, so a phone or tablet can show it.
 *
 * Capacity used to be the one scope with no route of its own: on desktop the
 * pane sat beside the list, but below desktop the shell gives the single band
 * to the list (`app.list` in `AppShell`'s matrix) whenever nothing is open, so
 * the push-level radios, the runner ceiling and the products table were
 * unreachable — and the stack's own "Runner ceiling" / "Products" links were
 * hash anchors into that hidden pane (2026-10-01 Delivery re-critique, P0).
 * The resolver discriminates on param NAMES, so the pane carries one:
 * `capacityPane`, whose only value is the governor's controls.
 */
export const CAPACITY_PANE = 'governor'
export const CAPACITY_ROUTE = `${DELIVERY_PATH}/capacity/:capacityPane`

export type DeliveryScope = 'automations' | 'ci' | 'capacity'
export const DELIVERY_SCOPES: readonly DeliveryScope[] = ['automations', 'ci', 'capacity']

export function deliveryScopeForSearch(search: string): DeliveryScope {
  const v = new URLSearchParams(search).get('scope')
  return (DELIVERY_SCOPES as readonly string[]).includes(v ?? '') ? (v as DeliveryScope) : 'automations'
}

/** The scope the list should show: a detail path names its own scope; on the
 *  bare section path `?scope=` decides. */
export function deliveryScope(pathname: string, search: string): DeliveryScope {
  if (pathname.startsWith(`${DELIVERY_PATH}/ci/`)) return 'ci'
  if (pathname.startsWith(`${DELIVERY_PATH}/automations/`)) return 'automations'
  if (pathname.startsWith(`${DELIVERY_PATH}/capacity/`)) return 'capacity'
  return deliveryScopeForSearch(search)
}

export function deliveryPath(scope: DeliveryScope = 'automations'): string {
  return scope === 'automations' ? DELIVERY_PATH : `${DELIVERY_PATH}?scope=${scope}`
}
export const automationPath = (id: string) => `${DELIVERY_PATH}/automations/${encodeURIComponent(id)}`
/** `repo` is `owner/name`; each half is one path segment. */
export const ciRepoPath = (repo: string) => {
  const [owner, name] = repo.split('/')
  return `${DELIVERY_PATH}/ci/${encodeURIComponent(owner ?? '')}/${encodeURIComponent(name ?? '')}`
}
/** The Capacity pane, open as an item; `hash` names a section inside it
 *  (`runner`, `products`) the way the stack's anchors always have. */
export const capacityPath = (hash?: string) =>
  `${DELIVERY_PATH}/capacity/${CAPACITY_PANE}${hash ? `#${hash}` : ''}`

export type DeliveryOpenItem =
  | { kind: 'automation'; automationId: string }
  | { kind: 'ciRepo'; repo: string }
  | { kind: 'capacity' }

// Takes only `params`, unlike `roadmapOpenItem`: Delivery has no search-only
// open item today (unlike Roadmap's inbox scope), so there is nothing for a
// second `search` parameter to read. A one-arg function still satisfies the
// two-arg `OpenItemResolver` type it is assigned to in `shell/openItems.ts` —
// JS ignores a caller's extra argument, and TS's function-type compatibility
// allows fewer parameters than the target type declares.
export function deliveryOpenItem(
  params: Readonly<Record<string, string | undefined>>,
): DeliveryOpenItem | null {
  const { automationId, owner, repo, capacityPane } = params
  if (automationId) return { kind: 'automation', automationId }
  if (owner && repo) return { kind: 'ciRepo', repo: `${owner}/${repo}` }
  // An unknown pane name is not an item (the same rule `DrawerHost` applies to
  // `?drawer=`): the page falls back to the scope, never to an empty pane.
  if (capacityPane === CAPACITY_PANE) return { kind: 'capacity' }
  return null
}

export const automationKey = (id: string) => `automation:${id}`
export const ciRepoKey = (repo: string) => `ci:${repo}`
/** The Capacity scope has no rows to highlight; the key exists so the open
 *  item is still keyed like the others. */
export const CAPACITY_KEY = 'capacity'

/** `CardFrame` id namespaces for the list column's two row lists, the ones
 *  `ui/list/useRovingRows` looks rows up under. Named here, beside the keys,
 *  because two parties must agree on each: the scope body that owns the list
 *  and the row that mints the id (the roadmap's `TREE_ID_SCOPE` precedent). */
export const AUTOMATIONS_ID_SCOPE = 'delivery-automations'
export const CI_ID_SCOPE = 'delivery-ci'

/** The list-row key an open item highlights. */
export function keyForDeliveryItem(item: DeliveryOpenItem | null): string | null {
  if (!item) return null
  if (item.kind === 'automation') return automationKey(item.automationId)
  if (item.kind === 'ciRepo') return ciRepoKey(item.repo)
  return CAPACITY_KEY
}
