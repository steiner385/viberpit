// The control-band incident queue (#804) — the human end of the SDLC Maintain
// loop. A deterministic monitor files each metric breach here; this view is where
// a human triages it now, schedules it, or dismisses it with a reason.
//
// NOT the PM triage inbox (`features/…` + `QK.triage`), which holds AI proposals
// about classifying work items. Same word, unrelated data — everything here says
// "incident" so the two stay legible apart.
import { useState } from 'react'
import { useIncidents, useIncidentAction } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { Incident, IncidentAction } from '@/api/sdlc'
import { Badge, type Tone } from '@/ui/Badge'
import { Input } from '@/ui/Input'

/** Statuses that mean the incident is still someone's problem. `dismissed` and
 *  `intake` are settled — one by a human, one by SDLC Intake adopting it — so
 *  they fall out of the default view rather than accumulating in it forever. */
const LIVE_STATUSES = new Set(['open', 'diagnosing', 'scheduled', 'queued'])

const STATUS_LABEL: Record<string, string> = {
  open: 'Open',
  diagnosing: 'Diagnosing',
  scheduled: 'Scheduled',
  queued: 'Queued for intake',
  intake: 'Adopted by intake',
  dismissed: 'Dismissed',
}

/** What each Western Electric rule actually says, in words. The queue is read by
 *  someone deciding whether to spend an afternoon on this, and "we2" alone does
 *  not help them. */
const RULE_LABEL: Record<string, string> = {
  we1: '1 point beyond 3σ',
  we2: '2 of 3 beyond 2σ',
  we3: '4 of 5 beyond 1σ',
  we4: '8 consecutive on one side',
}

/** Severity reads at a glance: 3σ is the tier that opens a PR, 1σ is a note. */
const SIGMA_TONE: Record<number, Tone> = { 3: 'danger', 2: 'warning' }

const STATUS_TONE: Record<string, Tone> = {
  queued: 'accent', diagnosing: 'warning', intake: 'success',
}

/** Snooze options for `schedule`, in hours. */
const SNOOZE_HOURS: ReadonlyArray<[string, number]> = [
  ['1 hour', 1],
  ['tomorrow', 24],
  ['next week', 24 * 7],
]

function formatValue(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return '—'
  // 3 significant figures: these are cycle-time hours and failure-rate fractions,
  // where the magnitude carries the meaning and the trailing digits do not.
  return Number(n.toPrecision(3)).toString()
}

function IncidentRow({ incident }: { incident: Incident }) {
  const act = useIncidentAction()
  const [dismissing, setDismissing] = useState(false)
  const [reason, setReason] = useState('')
  const settled = incident.status === 'intake' || incident.status === 'dismissed'

  const run = (action: IncidentAction, extra: Record<string, string> = {}) =>
    act.mutate({ id: incident.id, action, ...extra })

  return (
    <li className="border-t border-edge py-2 first:border-t-0 first:pt-0"
        aria-label={incident.summary ?? incident.key ?? incident.id}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SIGMA_TONE[incident.sigma ?? 0] ?? 'neutral'}>{incident.sigma ?? '?'}σ</Badge>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-medium text-ink">{incident.metric ?? 'unknown metric'}</span>
          {incident.repo && <span className="text-ink-muted">{incident.repo}</span>}
          <Badge tone={STATUS_TONE[incident.status ?? ''] ?? 'neutral'}>
            {STATUS_LABEL[incident.status ?? ''] ?? incident.status}
          </Badge>
        </div>
        {(incident.occurrences ?? 1) > 1 && (
          <span className="ml-auto text-xs tabular-nums text-ink-muted"
                title="times this breach has been re-observed">
            ×{incident.occurrences}
          </span>
        )}
      </div>

      <p className="mt-1.5 text-xs tabular-nums text-ink-muted">
        <strong className="text-ink">{formatValue(incident.value)}</strong>
        {' vs baseline '}
        {formatValue(incident.baselineMean)} ± {formatValue(incident.baselineStddev)}
        {incident.baselineN ? ` (n=${incident.baselineN})` : ''}
        {incident.observedAt ? ` on ${incident.observedAt}` : ''}
        {incident.rule && (
          <span className="ml-2 rounded-md border border-edge px-1.5"
                title={`Western Electric rule ${incident.rule}`}>
            {RULE_LABEL[incident.rule] ?? incident.rule}
          </span>
        )}
      </p>

      {incident.status === 'dismissed' && incident.dismissedReason && (
        <p className="mt-1.5 text-xs text-ink-muted">Dismissed: {incident.dismissedReason}</p>
      )}
      {incident.status === 'scheduled' && incident.scheduledFor && (
        <p className="mt-1.5 text-xs text-ink-muted">
          Scheduled for {new Date(incident.scheduledFor).toLocaleString()}
        </p>
      )}
      {incident.status === 'intake' && incident.initiativeSlug && (
        <p className="mt-1.5 text-xs text-ink-muted">
          Adopted as initiative <code>{incident.initiativeSlug}</code>
        </p>
      )}

      {!settled && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" className="btn-ghost" disabled={act.isPending}
                  onClick={() => run('triage_now')}
                  title="Queue this incident for SDLC Intake — it becomes an intent.md PR">
            Triage now
          </button>
          {SNOOZE_HOURS.map(([label, hours]) => (
            <button key={label} type="button" className="btn-ghost" disabled={act.isPending}
                    onClick={() => run('schedule', {
                      scheduledFor: new Date(Date.now() + hours * 3600_000).toISOString(),
                    })}
                    title={`Snooze this incident until ${label}`}>
              Snooze {label}
            </button>
          ))}
          {dismissing ? (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                if (!reason.trim()) return
                run('dismiss', { reason: reason.trim() })
              }}
            >
              <Input size="sm" className="w-56" aria-label="dismissal reason"
                     placeholder="why is this not a problem?"
                     value={reason} onChange={(e) => setReason(e.target.value)} />
              {/* The reason is required by the API, so the button gates on it here
                  rather than sending a request that can only 422. */}
              <button type="submit" className="btn-ghost"
                      disabled={!reason.trim() || act.isPending}>
                Dismiss
              </button>
              <button type="button" className="btn-ghost"
                      onClick={() => { setDismissing(false); setReason('') }}>
                Cancel
              </button>
            </form>
          ) : (
            <button type="button" className="btn-ghost" disabled={act.isPending}
                    onClick={() => setDismissing(true)}>
              Dismiss…
            </button>
          )}
        </div>
      )}

      {act.isError && (
        <p role="alert" className="text-sm text-danger">
          {extractMutationErrorMessage(act.error, 'Could not update this incident')}
        </p>
      )}
    </li>
  )
}

/**
 * The queue. Renders nothing at all when there are no live incidents — an empty
 * "no incidents" panel above the initiative board would be permanent furniture
 * on a healthy system, which is exactly when it has least to say.
 */
export function IncidentQueue({
  repos, title = 'Control-band incidents',
}: { repos?: readonly string[]; title?: string } = {}) {
  const { data: incidents, isError } = useIncidents()
  const [showSettled, setShowSettled] = useState(false)
  const all = (incidents ?? []).filter((i) => !repos || (i.repo !== null && repos.includes(i.repo)))
  const live = all.filter((i) => LIVE_STATUSES.has(i.status ?? ''))
  const settledCount = all.length - live.length
  const shown = showSettled ? all : live

  if (isError) {
    return <p className="stale" role="status">Could not load control-band incidents.</p>
  }
  if (all.length === 0) return null

  return (
    <section className="mb-4 rounded-md border border-edge bg-surface p-3"
             aria-label={title}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="m-0 flex items-center gap-2 font-medium text-ink">
          {title}
          {live.length > 0 && <Badge tone="warning" count={live.length} />}
        </h3>
        {settledCount > 0 && (
          <button type="button" className="btn-ghost"
                  onClick={() => setShowSettled((v) => !v)}>
            {showSettled ? 'Hide' : `Show ${settledCount} settled`}
          </button>
        )}
      </div>
      {shown.length === 0
        ? <p className="text-sm text-ink-muted">No open incidents — every watched band is in control.</p>
        : <ul className="list-none">
            {shown.map((i) => <IncidentRow key={i.id} incident={i} />)}
          </ul>}
    </section>
  )
}
