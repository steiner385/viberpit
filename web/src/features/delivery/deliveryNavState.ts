/**
 * Where the Delivery section lives and what is open — the one module that
 * spells these paths (finish-line spec §4.2), modelled on
 * `features/roadmap/roadmapNavState.ts`. The route PARAM NAMES are the contract:
 * the open-item resolver discriminates on them alone (`automationId`,
 * `owner` + `repo`), never on `id`, so a Delivery route can never be mistaken
 * for a live session.
 */
export const DELIVERY_PATH = '/delivery'
export const AUTOMATION_ROUTE = `${DELIVERY_PATH}/automations/:automationId`
export const CI_REPO_ROUTE = `${DELIVERY_PATH}/ci/:owner/:repo`

export type DeliveryScope = 'automations' | 'ci'
export const DELIVERY_SCOPES: readonly DeliveryScope[] = ['automations', 'ci']

export function deliveryScopeForSearch(search: string): DeliveryScope {
  const v = new URLSearchParams(search).get('scope')
  return (DELIVERY_SCOPES as readonly string[]).includes(v ?? '') ? (v as DeliveryScope) : 'automations'
}

/** The scope the list should show: a detail path names its own scope; on the
 *  bare section path `?scope=` decides. */
export function deliveryScope(pathname: string, search: string): DeliveryScope {
  if (pathname.startsWith(`${DELIVERY_PATH}/ci/`)) return 'ci'
  if (pathname.startsWith(`${DELIVERY_PATH}/automations/`)) return 'automations'
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

export type DeliveryOpenItem =
  | { kind: 'automation'; automationId: string }
  | { kind: 'ciRepo'; repo: string }

// Takes only `params`, unlike `roadmapOpenItem`: Delivery has no search-only
// open item today (unlike Roadmap's inbox scope), so there is nothing for a
// second `search` parameter to read. A one-arg function still satisfies the
// two-arg `OpenItemResolver` type it is assigned to in `shell/openItems.ts` —
// JS ignores a caller's extra argument, and TS's function-type compatibility
// allows fewer parameters than the target type declares.
export function deliveryOpenItem(
  params: Readonly<Record<string, string | undefined>>,
): DeliveryOpenItem | null {
  const { automationId, owner, repo } = params
  if (automationId) return { kind: 'automation', automationId }
  if (owner && repo) return { kind: 'ciRepo', repo: `${owner}/${repo}` }
  return null
}

export const automationKey = (id: string) => `automation:${id}`
export const ciRepoKey = (repo: string) => `ci:${repo}`

/** The list-row key an open item highlights. */
export function keyForDeliveryItem(item: DeliveryOpenItem | null): string | null {
  if (!item) return null
  return item.kind === 'automation' ? automationKey(item.automationId) : ciRepoKey(item.repo)
}
