import { useState } from 'react'
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

  const [draft, setDraft] = useState({ name: '', endpoint: '' })
  const [addError, setAddError] = useState<string | null>(null)

  const patch = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Partial<FleetHost> }) =>
      api.put<FleetHost>(`/fleet/hosts/${encodeURIComponent(name)}`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['fleet', 'hosts'] }),
  })

  const add = useMutation({
    mutationFn: (body: { name: string; endpoint: string }) =>
      api.post<FleetHost>('/fleet/hosts', body),
    onSuccess: () => {
      setDraft({ name: '', endpoint: '' })
      setAddError(null)
      void queryClient.invalidateQueries({ queryKey: ['fleet', 'hosts'] })
    },
    onError: (err: unknown) => {
      const body = (err as { body?: { detail?: { message?: string } } })?.body
      setAddError(body?.detail?.message ?? 'Failed to add host')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.del(`/fleet/hosts/${encodeURIComponent(name)}`),
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
