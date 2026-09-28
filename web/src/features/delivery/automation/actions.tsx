/**
 * The actionable-items list: the Overview's preview and the Needs you tab's full list.
 * Moved verbatim (behaviour) from the deleted `automations/AutomationsTab.tsx`'s
 * `AutomationActions`/`ActionButton` — only the classes changed, to `ui/`
 * primitives on tokens.
 */
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useAutomationActionable, useAutomationTrigger } from '@/api/hooks'
import { fetchPrDoctorJob, type ActionSpec } from '@/api/automations'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { SectionHeading } from '@/ui/SectionHeading'

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

/** Rows the Overview preview shows before handing off to the Needs you tab
 *  (docs/PAGE-TABS.md §5: a preview is at most three rows + "See all N"). */
export const PREVIEW_ROWS = 3

export function AutomationActions({ automationId, enabled, variant, onSeeAll }: {
  automationId: string
  enabled: boolean
  /** `preview`: the Overview's notice — heading, three rows, "See all N",
   *  nothing at all when empty. `full`: the Needs you tab — every row on the
   *  page scroll, a sentence when empty. */
  variant: 'preview' | 'full'
  /** Preview only: switch to the tab that owns the full list. */
  onSeeAll?: () => void
}) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  if (list.length === 0) {
    return variant === 'full' ? <p className="text-sm text-ink-muted">Nothing needs you.</p> : null
  }
  const rows = variant === 'preview' ? list.slice(0, PREVIEW_ROWS) : list
  const items = (
    <ul className="space-y-1.5 text-sm">
      {rows.map((item) => (
        <li key={item.id} className="flex items-center gap-2">
          <span className="flex-1 truncate">
            <span className="text-ink-muted tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
            {item.title}
          </span>
          {item.actions.length === 0 && item.status && (
            <span className="text-xs text-ink-muted">{item.status}</span>
          )}
          {item.actions.map((action) => (
            <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
          ))}
        </li>
      ))}
    </ul>
  )
  if (variant === 'full') return items
  return (
    <section className="space-y-2">
      <SectionHeading>Actionable items</SectionHeading>
      <div className="rounded-md border border-edge p-2">{items}</div>
      {list.length > PREVIEW_ROWS && (
        <div>
          <Button size="sm" variant="ghost" onClick={onSeeAll}>See all {list.length}</Button>
        </div>
      )}
    </section>
  )
}
