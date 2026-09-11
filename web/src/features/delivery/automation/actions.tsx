/**
 * The Status tab's "Actionable items" list and its per-item action buttons.
 * Moved verbatim (behaviour) from `automations/AutomationsTab.tsx`'s
 * `AutomationActions`/`ActionButton` — only the classes changed, to `ui/`
 * primitives on tokens.
 */
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useAutomationActionable, useAutomationTrigger } from '@/api/hooks'
import { fetchPrDoctorJob, type ActionSpec } from '@/api/automations'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'

function outcomeToast(r: { level: string; message: string }) {
  if (r.level === 'ok') toast.success(r.message)
  else if (r.level === 'warn') toast.warning(r.message)
  else toast.error(r.message)
}

// Async job phases → button label (issue #668). Shown while the background
// clone→assess→spawn pipeline runs so the user sees which phase is slow.
const PHASE_LABEL: Record<string, string> = {
  queued: 'Queued…', cloning: 'Cloning…', assessing: 'Assessing…', spawning: 'Spawning…',
}
const JOB_TERMINAL = new Set(['ready', 'error', 'deferred'])
const JOB_POLL_MS = 750

type Outcome = { level: string; message: string }

// Poll an async trigger job (issue #668) until it settles, reporting each phase
// to `onPhase`. Returns the final outcome, or a feed fallback if the job
// vanished (evicted / daemon restart → 404). `alive` lets the caller abandon the
// loop when the button unmounts.
async function pollJob(
  jobId: string, onPhase: (phase: string) => void, alive: () => boolean,
): Promise<Outcome> {
  while (alive()) {
    let job
    try {
      job = await fetchPrDoctorJob(jobId)
    } catch {
      return { level: 'warn', message: 'Lost track of the job — check the activity feed.' }
    }
    onPhase(job.phase)
    if (JOB_TERMINAL.has(job.status)) return { level: job.level, message: job.message }
    await new Promise((res) => setTimeout(res, JOB_POLL_MS))
  }
  return { level: 'warn', message: 'Cancelled' }
}

export function ActionButton(
  { item, action, automationId }:
  { item: { id: string }; action: ActionSpec; automationId: string },
) {
  const trigger = useAutomationTrigger(automationId)
  const [armed, setArmed] = useState(false)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  // Non-null phase while an async trigger (issue #668) is in flight.
  const [phase, setPhase] = useState<string | null>(null)
  // Flipped false on unmount so an in-flight poll loop abandons quietly.
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const busy = trigger.isPending || phase !== null

  useEffect(() => {
    if (!outcome) return
    const id = setTimeout(() => setOutcome(null), 4000)
    return () => clearTimeout(id)
  }, [outcome])

  // Auto-disarm a risky confirm so a user who reconsiders isn't left with a
  // sticky "Confirm?" — mirrors the outcome-badge auto-clear (#466).
  useEffect(() => {
    if (!armed) return
    const id = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(id)
  }, [armed])

  async function fire() {
    setArmed(false)
    const r = await trigger.mutateAsync({ item_id: item.id, kind: action.kind })
    // Async path (issue #668): poll the job for live phases, then settle. The
    // trigger RPC now returns in milliseconds instead of blocking 12–41 s.
    if (r.job_id && r.status === 'queued') {
      setPhase(r.phase ?? 'queued')
      const final = await pollJob(r.job_id, setPhase, () => alive.current)
      if (!alive.current) return
      setPhase(null)
      setOutcome(final)
      outcomeToast(final)
      return
    }
    setOutcome({ level: r.level, message: r.message })
    outcomeToast(r)
  }

  function onClick() {
    if (action.style === 'risky' && !armed) { setArmed(true); return }
    void fire()
  }

  // While a job runs, show its live phase; otherwise the generic pending label.
  const workingLabel = phase ? (PHASE_LABEL[phase] ?? 'Working…') : 'Working…'
  return (
    <span className="inline-flex items-center gap-2">
      <Button
        type="button"
        size="sm"
        variant={action.style === 'risky' ? 'danger' : 'secondary'}
        disabled={busy}
        onClick={onClick}
      >
        {busy ? workingLabel : armed ? `Confirm ${action.label}?` : action.label}
      </Button>
      {armed && !busy && (
        <Button type="button" size="sm" variant="secondary" aria-label={`Cancel ${action.label}`} onClick={() => setArmed(false)}>
          Cancel
        </Button>
      )}
      {outcome && (
        <Badge tone={outcome.level === 'ok' ? 'success' : outcome.level === 'warn' ? 'warning' : 'danger'}>
          {outcome.message}
        </Badge>
      )}
    </span>
  )
}

export function AutomationActions({ automationId, enabled }: { automationId: string; enabled: boolean }) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Actionable items</h4>
      {list.length === 0
        ? <p className="text-sm text-ink-muted">No actionable items.</p>
        : (
          <div className="max-h-48 overflow-y-auto rounded-md border border-edge p-2">
            <ul className="space-y-1.5 text-sm">
              {list.map((item) => (
                <li key={item.id} className="flex items-center gap-2">
                  <span className="flex-1 truncate">
                    <span className="text-ink-muted tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
                    {item.title}
                  </span>
                  {item.actions.map((action) => (
                    <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
                  ))}
                </li>
              ))}
            </ul>
          </div>
        )}
    </section>
  )
}
