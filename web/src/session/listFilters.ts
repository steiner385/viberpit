/**
 * Status / Project / Environment filtering for the session list.
 *
 * Pure shape + predicate. No React, no storage: persistence lives in
 * `lib/sessionOrder.ts` (the module that already owns `ccswitch.sessionOrder`),
 * which stores these under the SAME namespaced key rather than inventing a
 * third mechanism for one list's state (DEC-8).
 *
 * ## Why the option domains differ per filter
 *
 * `status` is a CLOSED vocabulary — the {@link StatusTier} set — so every tier
 * stays selectable even when no session currently sits in it. Filtering to
 * "Failed" with zero red sessions must show an EMPTY list, not
 * silently drop the filter; the control still shows the selection, so the user
 * can undo it.
 *
 * `project` and `environment` are OPEN, data-derived domains (repos among the
 * live sessions; names in the fleet host registry). A stored value there can
 * stop existing — a host retired from the fleet, a repo no session uses any
 * more. Such a value must fall back to "no filter", because a filter that
 * matches nothing AND is not offered by the control empties the list with no
 * way to recover from the UI: the control that would clear it is rendered from
 * the same empty set. {@link sanitizeFilters} is that guard.
 */
import type { Session } from '@/api/types'
import { TIER_RANK, TIER_LABEL, type StatusTier } from './statusTier'
import { triageTier } from './triageTier'
import { formatRepoLabel } from '@/lib/repoLabel'

export interface ListFilters {
  /** Session tier — see session/triageTier. Null = no filter. */
  status: string | null
  /** GitHub nameWithOwner, matching Session.repo. */
  project: string | null
  /** Fleet host name, matching Session.host. */
  environment: string | null
}

export const NO_FILTERS: ListFilters = { status: null, project: null, environment: null }

/** One selectable value plus the text a control renders for it. */
export interface FilterOption {
  value: string
  label: string
}

/** The values each filter may legally hold right now. */
export interface FilterOptions {
  status: FilterOption[]
  project: FilterOption[]
  environment: FilterOption[]
}

/** Every tier, ordered by triage urgency. Derived from TIER_RANK so the tier
 *  vocabulary is never restated as a parallel hand-written list. */
const STATUS_OPTIONS: FilterOption[] = (Object.keys(TIER_RANK) as StatusTier[])
  .sort((a, b) => TIER_RANK[a] - TIER_RANK[b])
  .map((tier) => ({ value: tier, label: TIER_LABEL[tier] }))

/**
 * The project a session belongs to, or null when it belongs to none.
 *
 * Routed through `formatRepoLabel` so "which repo is this, as text" is decided
 * in exactly one place: a scratch session (repo null/absent/blank) has no
 * project, so it is EXCLUDED by any active project filter and never appears as
 * an option. `project` is a nameWithOwner or nothing — there is no sentinel for
 * "scratch only", by design.
 */
function projectOf(session: Session): string | null {
  const label = formatRepoLabel(session.repo)
  return label.isScratch ? null : label.text
}

/** The fleet host a session ran on, or null (pre-fleet rows, unregistered
 *  endpoints). Null is excluded by any active environment filter, same as
 *  a repo-less session is by a project filter. */
function environmentOf(session: Session): string | null {
  const host = session.host?.trim()
  return host ? host : null
}

const byLabel = (a: FilterOption, b: FilterOption): number =>
  a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })

/**
 * What the filter controls may offer right now.
 *
 * `hostNames` is the fleet registry (`FleetHost.name`) — NOT the hosts seen on
 * sessions. A configured host running nothing is a legitimate choice that
 * yields an empty list; a host missing from the registry is a stale value.
 */
export function deriveFilterOptions(
  sessions: readonly Session[],
  hostNames: readonly string[] = [],
): FilterOptions {
  const projects = new Set<string>()
  for (const s of sessions) {
    const p = projectOf(s)
    if (p) projects.add(p)
  }
  const hosts = new Set<string>()
  for (const name of hostNames) {
    const trimmed = name.trim()
    if (trimmed) hosts.add(trimmed)
  }
  return {
    status: [...STATUS_OPTIONS],
    project: [...projects].map((value) => ({ value, label: value })).sort(byLabel),
    environment: [...hosts].map((value) => ({ value, label: value })).sort(byLabel),
  }
}

/** Coerce anything parsed out of storage into the ListFilters shape. Anything
 *  that is not a non-empty string becomes null ("no filter"). Domain validation
 *  is {@link sanitizeFilters}' job, not this one's. */
export function coerceFilters(raw: unknown): ListFilters {
  if (typeof raw !== 'object' || raw === null) return { ...NO_FILTERS }
  const rec = raw as Record<string, unknown>
  const one = (key: keyof ListFilters): string | null => {
    const v = rec[key]
    return typeof v === 'string' && v.trim() !== '' ? v.trim() : null
  }
  return { status: one('status'), project: one('project'), environment: one('environment') }
}

/**
 * Drop any filter value the options no longer offer. This is the recovery path
 * for a retired fleet host or a repo that left the session set — without it a
 * stale stored value filters the whole list away and the control that would
 * clear it has nothing to render.
 */
export function sanitizeFilters(f: ListFilters, options: FilterOptions): ListFilters {
  const keep = (value: string | null, opts: readonly FilterOption[]): string | null =>
    value !== null && opts.some((o) => o.value === value) ? value : null
  return {
    status: keep(f.status, options.status),
    project: keep(f.project, options.project),
    environment: keep(f.environment, options.environment),
  }
}

/** True when at least one filter is narrowing the list. */
export function isFiltered(f: ListFilters): boolean {
  return f.status !== null || f.project !== null || f.environment !== null
}

/**
 * Keep the sessions matching EVERY active filter (AND). A null filter means
 * "all". Order is preserved — grouping and sorting stay ordering's job.
 */
export function filterSessions(sessions: readonly Session[], f: ListFilters): Session[] {
  if (!isFiltered(f)) return [...sessions]
  return sessions.filter(
    (s) =>
      (f.status === null || triageTier(s) === f.status)
      && (f.project === null || projectOf(s) === f.project)
      && (f.environment === null || environmentOf(s) === f.environment),
  )
}
