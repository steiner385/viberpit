import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import { QK, usePendingDeploy, useFleetHostPatch, useSettings } from '@/api/hooks'
import { settingsSectionPath } from '@/shell/navDefs'
import { useConfirm } from '@/ui/useConfirm'
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
/** The host row: a two-column card below `sm`, the table row it always was above. */
const HOST_ROW = 'grid grid-cols-2 gap-x-3 gap-y-1 rounded-md border border-edge p-3 align-top sm:table-row sm:rounded-none sm:border-0 sm:border-t sm:p-0'
/** An editable field: 44px tall and wider on a phone, dense at a desk. */
const HOST_FIELD = 'min-h-target w-24 rounded border border-edge-strong bg-transparent px-2 sm:min-h-0 sm:w-16 sm:px-1'

/** One table cell that becomes a labelled line of the host card below `sm`.
 *  The label is aria-hidden: the table header still names the column for AT. */
function Cell({ label, children, className }: { label?: string; children: ReactNode; className?: string }) {
  return (
    <td className={cn('block min-w-0 py-0.5 sm:table-cell sm:py-1 sm:pr-2 sm:align-top', className)}>
      {label && <span aria-hidden="true" className="block text-xs text-ink-muted sm:hidden">{label}</span>}
      {children}
    </td>
  )
}

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
    return <span className="text-ink-muted">not advertised</span>
  }
  if (state.dispatchable) {
    return (
      <span className="text-success" title={state.cliVersion ?? undefined}>
        dispatchable
      </span>
    )
  }
  return (
    <div className="flex flex-col">
      <span className="text-warning">
        {state.available ? 'runner ready, edge refuses' : 'unavailable'}
      </span>
      <span className="text-xs text-ink-muted">
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
  // Row-level "max sessions" validation error, keyed by host name — set on an
  // invalid blur (kept local, never sent), cleared on the next valid blur.
  const [maxSessionsError, setMaxSessionsError] = useState<Record<string, string | null>>({})
  // Same shape for the weight field (#2032): the server clamps to 1–1000 but
  // would silently turn a blank or 0 into 1, so a bad entry is refused here.
  const [weightError, setWeightError] = useState<Record<string, string | null>>({})

  const patch = useFleetHostPatch()
  const { data: settings } = useSettings()
  const gc = settings?.effectiveDefaults.globalCap

  const add = useMutation({
    mutationFn: (body: { name: string; endpoint: string }) =>
      api.post<FleetHost>('/fleet/hosts', body),
    onSuccess: () => {
      setDraft({ name: '', endpoint: '' })
      setAddError(null)
      void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY })
      void queryClient.invalidateQueries({ queryKey: QK.capacity })
      void queryClient.invalidateQueries({ queryKey: QK.settings })
    },
    onError: (err: unknown) => {
      const body = (err as { body?: { detail?: { message?: string } } })?.body
      setAddError(body?.detail?.message ?? 'Failed to add host')
    },
  })

  const remove = useMutation({
    mutationFn: (name: string) => api.del(`/fleet/hosts/${encodeURIComponent(name)}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: FLEET_HOSTS_KEY })
      void queryClient.invalidateQueries({ queryKey: QK.capacity })
      void queryClient.invalidateQueries({ queryKey: QK.settings })
    },
  })

  // Both fleet-wide actions go through one gate (2026-09-30 Settings critique,
  // P1): removing a host was a 10×16px "✕" with no question; updating one was a
  // `window.confirm`. Removal takes the Typed tier — the operator types the host
  // name — because it is the only action here with nothing to undo.
  const { confirm, dialog } = useConfirm()
  const askRemove = async (h: FleetHost) => {
    const active = h.activeSessions ?? 0
    const ok = await confirm({
      title: `Remove ${h.name}?`,
      description: `${h.name} leaves the fleet: nothing new is dispatched to it and its cap and weight are forgotten.`
        + (active > 0 ? ` ${active} session${active === 1 ? ' is' : 's are'} running on it right now.` : '')
        + ' To drain it first, disable it instead.',
      confirmLabel: 'Remove host',
      typed: h.name,
    })
    if (ok) remove.mutate(h.name)
  }
  const askUpdate = async (h: FleetHost) => {
    const ok = await confirm({
      title: `Update ${h.name}?`,
      description: 'Its daemon restarts to the deployed version. Sessions on it are drained first, then resumed.',
      confirmLabel: 'Update host',
      tone: 'warning',
    })
    if (ok) upgrade.mutate(h.name)
  }

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
    return <p className="text-sm text-danger">Could not load the fleet.</p>
  }

  const hosts = data?.hosts ?? []

  return (
    <section className="space-y-3">
      {dialog}
      {isLoading && <p className="text-sm text-ink-muted">Loading…</p>}

      {!isLoading && hosts.length === 0 && (
        <p className="text-sm text-ink-muted">No hosts registered.</p>
      )}

      {hosts.length > 0 && (
        <>
        <div className="overflow-x-auto">
          {/* One DOM for every width (2026-09-30 Settings critique, P1: eleven
              columns ran 613px in a 358px viewport and every editable control —
              weight, cap, enabled, remove — sat off-screen). Below `sm` each row
              is a card: a two-column grid of labelled cells; from `sm` up the
              same cells are the table they always were. The labels are
              aria-hidden because the header row still names the columns. */}
          <table className="block w-full text-sm sm:table">
            <thead className="hidden sm:table-header-group">
              <tr className="text-left text-xs text-ink-muted">
                <th className="py-1 pr-2">Host</th>
                <th className="pr-2">Status</th>
                <th className="pr-2">Codex</th>
                <th className="pr-2">Sessions</th>
                <th className="pr-2">Load</th>
                <th className="pr-2">Free mem</th>
                <th className="pr-2">Version</th>
                <th className="pr-2">Weight (dispatch share)</th>
                <th className="pr-2">Cap</th>
                <th className="pr-2">Enabled</th>
                <th />
              </tr>
            </thead>
            <tbody className="block space-y-2 sm:table-row-group sm:space-y-0">
              {hosts.map((h) => (
                <tr key={h.name} className={HOST_ROW}>
                  <Cell className="col-span-2">
                    <div className="font-medium">{h.name}</div>
                    <div className="text-xs text-ink-muted break-all">
                      {h.endpoint}
                    </div>
                    {h.lastError && (
                      <div className="text-xs text-danger break-all">
                        {h.lastError}
                      </div>
                    )}
                  </Cell>
                  <Cell label="Status">{healthLabel(h)}</Cell>
                  <Cell label="Codex"><CodexCell host={h} /></Cell>
                  <Cell label="Sessions">{capacityLabel(h)}</Cell>
                  <Cell label="Load">{h.loadAvg?.toFixed(2) ?? '—'}</Cell>
                  <Cell label="Free mem">
                    {h.memAvailableMb ? `${Math.round(h.memAvailableMb / 1024)} GB` : '—'}
                  </Cell>
                  <Cell label="Version" className="col-span-2 font-mono text-xs">
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
                        onClick={() => void askUpdate(h)}
                        className="ml-2 inline-flex min-h-target items-center rounded border border-accent px-2 font-sans text-xs text-accent hover:bg-accent hover:text-accent-ink disabled:opacity-40 sm:min-h-0 sm:px-1.5 sm:py-0.5"
                      >
                        Update
                      </button>
                    )}
                    {h.upgrade?.state === 'running' && (
                      <div className="font-sans text-xs text-accent">
                        updating… {h.upgrade.step ?? ''}
                      </div>
                    )}
                    {h.upgrade?.state === 'failed' && (
                      <div className="font-sans text-xs text-danger break-all">
                        update failed: {h.upgrade.error}
                      </div>
                    )}
                    {h.upgrade?.state === 'done' && (
                      <div className="font-sans text-xs text-success">
                        updated
                      </div>
                    )}
                  </Cell>
                  <Cell label="Weight (dispatch share)">
                    <input
                      type="number"
                      min={1}
                      max={1000}
                      defaultValue={h.weight}
                      aria-label={`weight (dispatch share) for ${h.name}`}
                      aria-invalid={weightError[h.name] ? true : undefined}
                      className={HOST_FIELD}
                      onBlur={(e) => {
                        const raw = e.target.value.trim()
                        const weight = Number(raw)
                        if (raw === '' || !Number.isInteger(weight) || weight < 1 || weight > 1000) {
                          // Never patch a bad value — reset to what the server last
                          // confirmed and say why (mirrors the cap field below).
                          e.target.value = String(h.weight)
                          setWeightError((prev) => ({
                            ...prev, [h.name]: 'Weight must be a whole number from 1 to 1000',
                          }))
                          return
                        }
                        setWeightError((prev) => ({ ...prev, [h.name]: null }))
                        if (weight !== h.weight) patch.mutate({ name: h.name, body: { weight } })
                      }}
                    />
                    {weightError[h.name] && (
                      <p role="alert" className="text-xs text-danger">
                        {weightError[h.name]}
                      </p>
                    )}
                  </Cell>
                  <Cell label="Cap">
                    <input
                      // `type="text"`, not `number`: a number input SANITIZES
                      // non-numeric keystrokes at the DOM level (they never
                      // land in `.value` at all), so the custom "must be a
                      // whole number ≥ 1" validation below could never see
                      // — let alone report — an "abc" entry. `inputMode`
                      // still asks for the numeric keypad on mobile.
                      type="text"
                      inputMode="numeric"
                      defaultValue={h.maxSessions ?? ''}
                      aria-label={`cap for ${h.name}`}
                      aria-invalid={maxSessionsError[h.name] ? true : undefined}
                      className={HOST_FIELD}
                      onBlur={(e) => {
                        const raw = e.target.value.trim()
                        if (raw === '') {
                          setMaxSessionsError((prev) => ({ ...prev, [h.name]: null }))
                          if (h.maxSessions !== null) patch.mutate({ name: h.name, body: { maxSessions: null } })
                          return
                        }
                        const n = Number(raw)
                        if (!Number.isInteger(n) || n < 1) {
                          // Never patch a bad value — reset the field to what the
                          // server last confirmed and surface a row-level error
                          // instead of silently dropping the edit.
                          e.target.value = String(h.maxSessions ?? '')
                          setMaxSessionsError((prev) => ({
                            ...prev, [h.name]: 'Cap must be a whole number ≥ 1',
                          }))
                          return
                        }
                        setMaxSessionsError((prev) => ({ ...prev, [h.name]: null }))
                        if (n !== h.maxSessions) patch.mutate({ name: h.name, body: { maxSessions: n } })
                      }}
                    />
                    {maxSessionsError[h.name] && (
                      <p role="alert" className="text-xs text-danger">
                        {maxSessionsError[h.name]}
                      </p>
                    )}
                  </Cell>
                  <Cell label="Enabled">
                    {/* The 13×13 box rides inside a 44px label on a phone. */}
                    <label className="flex min-h-target items-center gap-2 sm:min-h-0">
                      <input
                        type="checkbox"
                        checked={h.enabled}
                        aria-label={`enable ${h.name}`}
                        onChange={(e) =>
                          patch.mutate({ name: h.name, body: { enabled: e.target.checked } })
                        }
                      />
                      <span aria-hidden="true" className="text-xs text-ink-muted sm:hidden">{h.enabled ? 'on' : 'off'}</span>
                    </label>
                  </Cell>
                  <Cell className="col-span-2 justify-self-end">
                    {/* A 10×16px "✕" at a desk; on a phone a labelled 44px target. */}
                    <button
                      type="button"
                      aria-label={`remove ${h.name}`}
                      title="Remove host"
                      disabled={remove.isPending}
                      onClick={() => void askRemove(h)}
                      className="inline-flex min-h-target items-center gap-1 rounded px-2 text-xs text-ink-muted hover:text-danger disabled:opacity-40 sm:min-h-0 sm:px-0"
                    >
                      ✕<span className="sm:sr-only"> Remove</span>
                    </button>
                  </Cell>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-ink-muted">
          Weight is a tiebreaker when several hosts have headroom; it is not a cap. Disabling a
          host removes it from dispatch without deleting it.
        </p>
        {gc && (
          <p className="text-xs text-ink-muted">
            {gc.physical === null
              ? 'Σ caps = unbounded (an enabled host has no cap) → '
              : `Σ caps = ${gc.physical} → `}
            <Link to={settingsSectionPath('capacity')} className="text-accent hover:underline">physical ceiling</Link>
          </p>
        )}
        </>
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
          <label className="text-xs text-ink-muted" htmlFor="fleet-add-name">
            Name
          </label>
          <input
            id="fleet-add-name"
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            placeholder="rocky"
            className="w-28 rounded border border-edge-strong bg-transparent px-2 py-1 text-sm"
          />
        </div>
        <div className="flex flex-col gap-0.5">
          <label className="text-xs text-ink-muted" htmlFor="fleet-add-endpoint">
            Endpoint
          </label>
          <input
            id="fleet-add-endpoint"
            value={draft.endpoint}
            onChange={(e) => setDraft((d) => ({ ...d, endpoint: e.target.value }))}
            placeholder="https://rocky:9443"
            className="w-56 rounded border border-edge-strong bg-transparent px-2 py-1 text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={add.isPending || !draft.name.trim() || !draft.endpoint.trim()}
          className="rounded bg-accent px-3 py-1 text-sm text-accent-ink hover:bg-accent/90 disabled:opacity-40"
        >
          {add.isPending ? 'Adding…' : 'Add host'}
        </button>
        {addError && (
          <p className="w-full text-xs text-danger">{addError}</p>
        )}
      </form>
    </section>
  )
}
