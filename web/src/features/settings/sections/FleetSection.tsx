import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { usePendingDeploy } from '@/api/hooks'
// The host shape, the query and its KEY live in `@/api/fleet` — one fetch,
// shared with the prompt bar's Runner dropdown (DEC-2). The four mutations
// below invalidate through `FLEET_HOSTS_KEY` rather than re-spelling
// `['fleet','hosts']`: an open-coded invalidation that drifts from the query
// key does not error, it just silently stops refreshing — a host the operator
// adds, enables or removes would sit there until a reload.
import {
  FLEET_HOSTS_KEY, engineReasonLabel, hostEngine, useFleetHosts, type FleetHost,
} from '@/api/fleet'

/** Prefix match, not ===: hosts abbreviate the same commit to different
 *  lengths (mirrors server.deploy.steps.short_sha_match, #788). */
function shaMatch(a: string, b: string): boolean {
  return a.length > 0 && b.length > 0 && (a.startsWith(b) || b.startsWith(a))
}

/** Tri-state on purpose: null means "never probed", NOT "down". */
function healthLabel(h: FleetHost): string {
  if (h.healthy === true) return 'healthy'
  if (h.healthy === false) return 'unhealthy'
  return 'not yet probed'
}

function capacityLabel(h: FleetHost): string {
  const active = h.activeSessions ?? 0
  return h.maxSessions ? `${active} / ${h.maxSessions}` : String(active)
}

/**
 * The Codex cell: what the RUNNER says, and what this EDGE will do about it.
 *
 * Tri-state on the same principle as `healthLabel`: absent (a runner that
 * predates `/info.engines`, or an edge that predates the key) is "not
 * advertised", NOT "unavailable" — the runner never claimed it could not.
 *
 * Both verdicts are shown because `available: true` with `dispatchable: false`
 * is a real state — a runner happily running Codex against an app-server schema
 * contract this edge is not pinned to — and which half is at fault decides
 * which machine gets upgraded.
 *
 * Nothing here touches host health: a runner with no Codex binary is a perfectly
 * good Claude runner, and colouring the row would send an operator to fix the
 * wrong thing.
 */
function CodexCell({ host }: { host: FleetHost }) {
  const state = hostEngine(host, 'codex')
  if (state === null || state.reason === 'engine_not_advertised') {
    return <span className="text-gray-500 dark:text-gray-400">not advertised</span>
  }
  if (state.dispatchable) {
    return (
      <span className="text-green-600 dark:text-green-400" title={state.cliVersion ?? undefined}>
        dispatchable
      </span>
    )
  }
  return (
    <div className="flex flex-col">
      <span className="text-amber-600 dark:text-amber-400">
        {state.available ? 'runner ready, edge refuses' : 'unavailable'}
      </span>
      <span className="text-xs text-gray-500 dark:text-gray-400">
        {engineReasonLabel(state.dispatchReason ?? state.reason)}
      </span>
    </div>
  )
}

/**
 * Fleet host registry: health, capacity, running sha, and the operator-tunable
 * fields (enabled / weight / max sessions).
 *
 * `sha` is shown per host because version skew between the edge and a remote
 * daemon is otherwise invisible until something breaks — on 2026-08-21 a stale
 * rocky daemon returned 409 for every repo listing and the only symptom was an
 * empty dropdown.
 */
export function FleetSection() {
  const queryClient = useQueryClient()
  // 15s, not the default 30s: this table shows live health and probe results.
  const { data, isLoading, isError } = useFleetHosts(15_000)
  // Newest known sha: the deploy's pending target when it reports one, else the
  // edge's own live sha. A host whose sha trails it (and that has ssh config)
  // can be updated in place.
  const { data: pending } = usePendingDeploy()
  const newestSha = pending?.targetSha || pending?.liveSha || null

  const [draft, setDraft] = useState({ name: '', endpoint: '' })
  const [addError, setAddError] = useState<string | null>(null)

  const patch = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Partial<FleetHost> }) =>
      api.put<FleetHost>(`/fleet/hosts/${encodeURIComponent(name)}`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY }),
  })

  const add = useMutation({
    mutationFn: (body: { name: string; endpoint: string }) =>
      api.post<FleetHost>('/fleet/hosts', body),
    onSuccess: () => {
      setDraft({ name: '', endpoint: '' })
      setAddError(null)
      void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY })
    },
    onError: (err: unknown) => {
      const body = (err as { body?: { detail?: { message?: string } } })?.body
      setAddError(body?.detail?.message ?? 'Failed to add host')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.del(`/fleet/hosts/${encodeURIComponent(name)}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY }),
  })

  const upgrade = useMutation({
    mutationFn: (name: string) =>
      api.post<{ started: boolean }>(`/fleet/hosts/${encodeURIComponent(name)}/upgrade`, {}),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY }),
  })

  /** Outdated AND upgradable in-app (ssh config set).
   *  Two ways to be outdated. The checkout HEAD may trail the newest known sha — compare
   *  against it rather than the process sha, since a deploy can advance the checkout
   *  without restarting (#859). But the checkout can also be bang up to date while the
   *  PROCESS still runs older code: exactly the "version skew" the probe fences on, and
   *  exactly what an Update (which restarts the daemon) fixes. Without this second case
   *  the one host that most needs the button is the one host that never shows it. */
  const canUpdate = (h: FleetHost): boolean => {
    const compareSha = h.checkoutSha ?? h.sha
    const checkoutTrails = Boolean(
      compareSha && newestSha && !shaMatch(compareSha, newestSha))
    const processSkewed = Boolean(h.lastError?.startsWith('version skew'))
    return Boolean(h.sshHost && h.repoPath) &&
      h.upgrade?.state !== 'running' &&
      (checkoutTrails || processSkewed)
  }

  if (isError) {
    return <p className="text-sm text-red-600 dark:text-red-400">Could not load the fleet.</p>
  }

  const hosts = data?.hosts ?? []

  return (
    <section className="space-y-3">
      <header>
        <h3 className="text-sm font-semibold">Fleet</h3>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Hosts that can run sessions. Weight multiplies a host&apos;s score when
          ranking; disabling a host removes it from dispatch without deleting it.
        </p>
      </header>

      {isLoading && <p className="text-sm text-gray-500">Loading…</p>}

      {!isLoading && hosts.length === 0 && (
        <p className="text-sm text-gray-500">No hosts registered.</p>
      )}

      {hosts.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 dark:text-gray-400">
                <th className="py-1 pr-2">Host</th>
                <th className="pr-2">Status</th>
                <th className="pr-2">Codex</th>
                <th className="pr-2">Sessions</th>
                <th className="pr-2">Load</th>
                <th className="pr-2">Free mem</th>
                <th className="pr-2">Version</th>
                <th className="pr-2">Weight</th>
                <th className="pr-2">Enabled</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {hosts.map((h) => (
                <tr key={h.name} className="border-t border-gray-200 dark:border-gray-700 align-top">
                  <td className="py-1 pr-2">
                    <div className="font-medium">{h.name}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 break-all">
                      {h.endpoint}
                    </div>
                    {h.lastError && (
                      <div className="text-xs text-red-600 dark:text-red-400 break-all">
                        {h.lastError}
                      </div>
                    )}
                  </td>
                  <td className="pr-2">{healthLabel(h)}</td>
                  <td className="pr-2"><CodexCell host={h} /></td>
                  <td className="pr-2">{capacityLabel(h)}</td>
                  <td className="pr-2">{h.loadAvg?.toFixed(2) ?? '—'}</td>
                  <td className="pr-2">
                    {h.memAvailableMb ? `${Math.round(h.memAvailableMb / 1024)} GB` : '—'}
                  </td>
                  <td className="pr-2 font-mono text-xs">
                    {h.sha ?? '—'}
                    {h.checkoutSha && h.sha && !shaMatch(h.checkoutSha, h.sha) && (
                      <div className="font-sans text-xs text-ink-muted">checkout {h.checkoutSha}</div>
                    )}
                    {canUpdate(h) && (
                      <button
                        type="button"
                        aria-label={`update ${h.name}`}
                        title={`Update ${h.name} to the deployed version`}
                        disabled={upgrade.isPending}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Update ${h.name} to the deployed version? Its daemon will restart.`,
                            )
                          )
                            upgrade.mutate(h.name)
                        }}
                        className="ml-2 rounded border border-blue-600 px-1.5 py-0.5 font-sans text-xs text-blue-600 hover:bg-blue-600 hover:text-white disabled:opacity-40 dark:border-blue-400 dark:text-blue-400"
                      >
                        Update
                      </button>
                    )}
                    {h.upgrade?.state === 'running' && (
                      <div className="font-sans text-xs text-blue-600 dark:text-blue-400">
                        updating… {h.upgrade.step ?? ''}
                      </div>
                    )}
                    {h.upgrade?.state === 'failed' && (
                      <div className="font-sans text-xs text-red-600 dark:text-red-400 break-all">
                        update failed: {h.upgrade.error}
                      </div>
                    )}
                    {h.upgrade?.state === 'done' && (
                      <div className="font-sans text-xs text-green-600 dark:text-green-400">
                        updated
                      </div>
                    )}
                  </td>
                  <td className="pr-2">
                    <input
                      type="number"
                      min={1}
                      max={1000}
                      defaultValue={h.weight}
                      aria-label={`weight for ${h.name}`}
                      className="w-16 rounded border border-gray-300 dark:border-gray-600 bg-transparent px-1"
                      onBlur={(e) => {
                        const weight = Number(e.target.value)
                        if (weight !== h.weight) patch.mutate({ name: h.name, body: { weight } })
                      }}
                    />
                  </td>
                  <td className="pr-2">
                    <input
                      type="checkbox"
                      checked={h.enabled}
                      aria-label={`enable ${h.name}`}
                      onChange={(e) =>
                        patch.mutate({ name: h.name, body: { enabled: e.target.checked } })
                      }
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      aria-label={`remove ${h.name}`}
                      title="Remove host"
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(h.name)}
                      className="text-xs text-gray-400 hover:text-red-600 dark:hover:text-red-400 disabled:opacity-40"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <form
        className="flex flex-wrap items-end gap-2 pt-1"
        onSubmit={(e) => {
          e.preventDefault()
          if (!draft.name.trim() || !draft.endpoint.trim()) return
          add.mutate({ name: draft.name.trim(), endpoint: draft.endpoint.trim() })
        }}
      >
        <div className="flex flex-col gap-0.5">
          <label className="text-xs text-gray-500 dark:text-gray-400" htmlFor="fleet-add-name">
            Name
          </label>
          <input
            id="fleet-add-name"
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            placeholder="rocky"
            className="w-28 rounded border border-gray-300 dark:border-gray-600 bg-transparent px-2 py-1 text-sm"
          />
        </div>
        <div className="flex flex-col gap-0.5">
          <label className="text-xs text-gray-500 dark:text-gray-400" htmlFor="fleet-add-endpoint">
            Endpoint
          </label>
          <input
            id="fleet-add-endpoint"
            value={draft.endpoint}
            onChange={(e) => setDraft((d) => ({ ...d, endpoint: e.target.value }))}
            placeholder="https://rocky:9443"
            className="w-56 rounded border border-gray-300 dark:border-gray-600 bg-transparent px-2 py-1 text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={add.isPending || !draft.name.trim() || !draft.endpoint.trim()}
          className="rounded bg-blue-600 px-3 py-1 text-sm text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {add.isPending ? 'Adding…' : 'Add host'}
        </button>
        {addError && (
          <p className="w-full text-xs text-red-600 dark:text-red-400">{addError}</p>
        )}
      </form>
    </section>
  )
}
