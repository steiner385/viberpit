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
    <li className="incident-row" aria-label={incident.summary ?? incident.key ?? incident.id}>
      <div className="incident-head">
        <span className={`incident-sigma incident-sigma-${incident.sigma ?? 0}`}>
          {incident.sigma ?? '?'}σ
        </span>
        <div className="incident-ident">
          <span className="incident-metric">{incident.metric ?? 'unknown metric'}</span>
          {incident.repo && <span className="incident-repo">{incident.repo}</span>}
          <span className={`incident-status incident-status-${incident.status}`}>
            {STATUS_LABEL[incident.status ?? ''] ?? incident.status}
          </span>
        </div>
        {(incident.occurrences ?? 1) > 1 && (
          <span className="incident-occurrences"
                title="times this breach has been re-observed">
            ×{incident.occurrences}
          </span>
        )}
      </div>

      <p className="incident-detail">
        <strong>{formatValue(incident.value)}</strong>
        {' vs baseline '}
        {formatValue(incident.baselineMean)} ± {formatValue(incident.baselineStddev)}
        {incident.baselineN ? ` (n=${incident.baselineN})` : ''}
        {incident.observedAt ? ` on ${incident.observedAt}` : ''}
        {incident.rule && (
          <span className="incident-rule" title={`Western Electric rule ${incident.rule}`}>
            {RULE_LABEL[incident.rule] ?? incident.rule}
          </span>
        )}
      </p>

      {incident.status === 'dismissed' && incident.dismissedReason && (
        <p className="incident-note">Dismissed: {incident.dismissedReason}</p>
      )}
      {incident.status === 'scheduled' && incident.scheduledFor && (
        <p className="incident-note">
          Scheduled for {new Date(incident.scheduledFor).toLocaleString()}
        </p>
      )}
      {incident.status === 'intake' && incident.initiativeSlug && (
        <p className="incident-note">
          Adopted as initiative <code>{incident.initiativeSlug}</code>
        </p>
      )}

      {!settled && (
        <div className="incident-actions">
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
              className="incident-dismiss"
              onSubmit={(e) => {
                e.preventDefault()
                if (!reason.trim()) return
                run('dismiss', { reason: reason.trim() })
              }}
            >
              <input aria-label="dismissal reason" placeholder="why is this not a problem?"
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
        <p role="alert" className="init-error">
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
export function IncidentQueue() {
  const { data: incidents, isError } = useIncidents()
  const [showSettled, setShowSettled] = useState(false)
  const all = incidents ?? []
  const live = all.filter((i) => LIVE_STATUSES.has(i.status ?? ''))
  const settledCount = all.length - live.length
  const shown = showSettled ? all : live

  if (isError) {
    return <p className="stale" role="status">Could not load control-band incidents.</p>
  }
  if (all.length === 0) return null

  return (
    <section className="incident-queue" aria-label="Control-band incidents">
      <div className="incident-queue-head">
        <h3 className="incident-queue-title">
          Control-band incidents
          {live.length > 0 && <span className="incident-count">{live.length}</span>}
        </h3>
        {settledCount > 0 && (
          <button type="button" className="btn-ghost"
                  onClick={() => setShowSettled((v) => !v)}>
            {showSettled ? 'Hide' : `Show ${settledCount} settled`}
          </button>
        )}
      </div>
      {shown.length === 0
        ? <p className="init-empty">No open incidents — every watched band is in control.</p>
        : <ul className="incident-list">
            {shown.map((i) => <IncidentRow key={i.id} incident={i} />)}
          </ul>}
    </section>
  )
}
