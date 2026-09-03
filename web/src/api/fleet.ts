// web/src/api/fleet.ts
//
// The fleet-host list, fetched in ONE place.
//
// `['fleet', 'hosts']` was open-coded SIX times — fetched in
// `settings/FleetSection.tsx` (the management table, 15s) and `Dashboard.tsx`
// (the DispatchBar's Runner dropdown, 30s), each with its own `api.get` call,
// its own cadence and its own copy of the `enabled` filter, and invalidated at
// four more sites in `FleetSection`'s add/patch/remove/upgrade mutations. Task 12
// needs a seventh (`shell/regions/PromptDock`, DEC-2: the Runner picker stops
// being phone-only).
//
// Readers and invalidators must share one spelling or the failure is SILENT:
// change the key here and a mismatched `invalidateQueries` still resolves, still
// logs nothing, and simply stops refreshing — an operator adds, enables or
// removes a host and the table does not move until they reload. So
// `FLEET_HOSTS_KEY` is exported for invalidation as much as for the query.
//
// `FleetHost` moved here from `settings/FleetSection.tsx` with it: the shape of
// an API response belongs beside the call that fetches it, not inside the
// component that happened to render it first.
import { useQuery } from '@tanstack/react-query'
import { api } from './client'

export interface HostUpgrade {
  state: 'idle' | 'running' | 'failed' | 'done'
  step: string | null
  error: string | null
  startedAt: string | null
  target: string | null
}

export interface FleetHost {
  name: string
  endpoint: string
  enabled: boolean
  weight: number
  maxSessions: number | null
  healthy: boolean | null
  activeSessions: number | null
  loadAvg: number | null
  memAvailableMb: number | null
  sha: string | null
  checkoutSha: string | null
  protocol: number | null
  lastProbeAt: string | null
  lastError: string | null
  sshHost: string | null
  repoPath: string | null
  upgrade: HostUpgrade | null
}

/** The query key, exported so invalidations cannot misspell it. */
export const FLEET_HOSTS_KEY = ['fleet', 'hosts'] as const

/**
 * Every configured host, healthy or not — the ONE query behind every Runner
 * dropdown and the Settings › Fleet table. Lives in its own module (not
 * `hooks.ts`) so the ~40 tests that hand-roll a `vi.mock('@/api/hooks')`
 * factory keep working without each one learning a new export.
 *
 * `refetchInterval` is a parameter because the two readers genuinely differ:
 * the settings table watches live health and probe results (15s), while a
 * dropdown of runner names does not (30s). Both cadences were already in the
 * tree; unifying the fetch is the point, not unifying the poll.
 */
export function useFleetHosts(refetchInterval = 30_000) {
  return useQuery<{ hosts: FleetHost[] }>({
    queryKey: FLEET_HOSTS_KEY,
    queryFn: () => api.get<{ hosts: FleetHost[] }>('/fleet/hosts'),
    refetchInterval,
  })
}

/** Hosts an operator has enabled — the only ones a session may be pinned to. */
export function enabledHosts(data: { hosts: FleetHost[] } | undefined): FleetHost[] {
  return (data?.hosts ?? []).filter((h) => h.enabled)
}

/**
 * The hosts a session may actually be dispatched to.
 *
 * The hook form for callers that only want the filtered list; `enabledHosts`
 * stays exported for callers that already hold the query result.
 */
export function useEnabledFleetHosts(): FleetHost[] {
  return enabledHosts(useFleetHosts().data)
}
