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
import type { AgentEngine } from './types'

/**
 * One engine's state on one runner (`server/api/fleet.py:_engines_wire`).
 *
 * TWO verdicts, and they genuinely differ: `available` is what the RUNNER says
 * about itself, `dispatchable` is what THIS edge is willing to do about it.
 * `available: true` with `dispatchable: false` is a real state — a runner
 * happily running Codex against an app-server schema contract this edge is not
 * pinned to — and an operator needs both halves to know which side to upgrade.
 *
 * `reason` / `dispatchReason` are typed tokens, not prose
 * (`binary_not_found`, `contract_hash_mismatch`, `engine_not_advertised`, …);
 * the UI maps them to plain language. There is deliberately no free-text detail
 * field: the server scrubs Codex detail strings wholesale, so shipping one
 * would render "[REDACTED]" to an operator.
 */
export interface HostEngineState {
  engine: AgentEngine
  /** The runner's own verdict. */
  available: boolean
  /** Why not available (or `engine_not_advertised` for a legacy runner). */
  reason: string | null
  cliVersion: string | null
  contractSha256: string | null
  /** Whether the runner has the external-token auth plane on. Null = unknown. */
  externalAuth: boolean | null
  /** The EDGE's verdict: may a launch for this engine be sent here? */
  dispatchable: boolean
  /** Why the edge would refuse; null when it would not. */
  dispatchReason: string | null
}

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
  /**
   * Per-engine state, one entry per engine the EDGE knows about (never only the
   * ones the runner mentioned). Optional because an edge that predates the
   * engine registry omits the key entirely — which is the same tri-state
   * `healthy` uses: absent means "not advertised", never "unavailable".
   */
  engines?: HostEngineState[]
}

/** One engine's row for a host, or null when the host never advertised it.
 *  The ONE reader of `FleetHost.engines`, so "absent" cannot come to mean
 *  different things in the fleet table and in the Runner picker. */
export function hostEngine(
  host: Pick<FleetHost, 'engines'>,
  engine: AgentEngine,
): HostEngineState | null {
  return host.engines?.find((e) => e.engine === engine) ?? null
}

/** Whether a launch for `engine` may be sent to this host.
 *
 *  A host with NO engine information at all is treated as Claude-only — the same
 *  rolling-upgrade default the server applies to a daemon that predates
 *  `/info.engines` (`server/sessions/host_engines.py:LEGACY_ENGINES`). */
export function hostDispatchable(
  host: Pick<FleetHost, 'engines'>,
  engine: AgentEngine,
): boolean {
  const state = hostEngine(host, engine)
  if (state) return state.dispatchable
  return !host.engines?.length && engine === 'claude_code'
}

/**
 * Plain language for the engine reason tokens — the ONE map.
 *
 * The same vocabulary appears in three places on the wire: a runner's own
 * `reason`, the edge's `dispatchReason` (those two plus `contract_hash_mismatch`),
 * and `/api/config`'s edge-wide `blockedReason`. They are one vocabulary, so
 * they get one translation: a second copy in the launch bar would drift from
 * the fleet table's the first time a token is added.
 */
export const ENGINE_REASON_LABELS: Record<string, string> = {
  disabled: 'not enabled on this runner',
  external_auth_disabled: 'external token auth is off',
  binary_not_found: 'agent binary not installed',
  version_probe_failed: 'version probe failed',
  unsupported_version: 'unsupported agent version',
  runtime_home_refused: 'runtime home refused',
  engine_not_advertised: 'not advertised (older runner)',
  contract_hash_mismatch: 'app-server contract differs from this edge',
  authority_unconfigured: 'no credential authority is configured',
}

/** Human text for a reason token, falling back to the token so an unknown one
 *  is still SHOWN rather than swallowed into a blank cell. */
export function engineReasonLabel(token: string | null | undefined): string {
  if (!token) return 'unavailable'
  return ENGINE_REASON_LABELS[token] ?? token
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

/**
 * The hosts a session may actually be dispatched to — the ones an operator has
 * enabled.
 *
 * This is the hook form of what `enabledHosts(data)` used to be. That helper
 * existed so `Dashboard` and `DesktopShell` could each filter the query result
 * they separately held; with one shell there is one dock (`shell/regions/
 * PromptDock`), one caller, and no reason to hand out an unfiltered list and a
 * filter to pair with it.
 */
export function useEnabledFleetHosts(): FleetHost[] {
  const { data } = useFleetHosts()
  return (data?.hosts ?? []).filter((h) => h.enabled)
}
