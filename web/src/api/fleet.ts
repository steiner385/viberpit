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

export const FLEET_HOSTS_KEY = ['fleet', 'hosts'] as const

/**
 * The fleet host registry — the ONE query behind every Runner dropdown and the
 * Settings › Fleet table. Lives in its own module (not `hooks.ts`) so the ~40
 * tests that hand-roll a `vi.mock('@/api/hooks', () => ({...}))` factory keep
 * working without each one learning a new export.
 *
 * Both shells must read this: the Runner dropdown vanished at ≥1024px because
 * only the mobile Dashboard fetched hosts (#807 wired one shell, not both).
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
