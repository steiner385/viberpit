import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'

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
  protocol: number | null
  lastProbeAt: string | null
  lastError: string | null
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
  const { data, isLoading, isError } = useQuery<{ hosts: FleetHost[] }>({
    queryKey: ['fleet', 'hosts'],
    queryFn: () => api.get<{ hosts: FleetHost[] }>('/fleet/hosts'),
    refetchInterval: 15_000,
  })

  const patch = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Partial<FleetHost> }) =>
      api.put<FleetHost>(`/fleet/hosts/${encodeURIComponent(name)}`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['fleet', 'hosts'] }),
  })

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
                <th className="pr-2">Sessions</th>
                <th className="pr-2">Load</th>
                <th className="pr-2">Free mem</th>
                <th className="pr-2">Version</th>
                <th className="pr-2">Weight</th>
                <th>Enabled</th>
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
                  <td className="pr-2">{capacityLabel(h)}</td>
                  <td className="pr-2">{h.loadAvg?.toFixed(2) ?? '—'}</td>
                  <td className="pr-2">
                    {h.memAvailableMb ? `${Math.round(h.memAvailableMb / 1024)} GB` : '—'}
                  </td>
                  <td className="pr-2 font-mono text-xs">{h.sha ?? '—'}</td>
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
                  <td>
                    <input
                      type="checkbox"
                      checked={h.enabled}
                      aria-label={`enable ${h.name}`}
                      onChange={(e) =>
                        patch.mutate({ name: h.name, body: { enabled: e.target.checked } })
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
