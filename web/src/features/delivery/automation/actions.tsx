/**
 * The actionable-items list: the Overview's preview and the Needs you tab's full list.
 * Moved verbatim (behaviour) from the deleted `automations/AutomationsTab.tsx`'s
 * `AutomationActions`/`ActionButton` — only the classes changed, to `ui/`
 * primitives on tokens.
 */
import { useEffect, useState } from 'react'
import { useAutomationActionable } from '@/api/hooks'
import { splitActionable, type ActionableItem, type ActionSpec } from '@/api/automations'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { SectionHeading } from '@/ui/SectionHeading'
import { useActionJobs, type ItemJob } from './useActionJobs'

// Async job phases → button label (issue #668). Shown while the background
// clone→assess→spawn pipeline runs so the user sees which phase is slow.
const PHASE_LABEL: Record<string, string> = {
  queued: 'Queued…', cloning: 'Cloning…', assessing: 'Assessing…', spawning: 'Spawning…',
}

/** One action's button. Its job and result are the row's (`useActionJobs`);
 *  only the risky-action confirm is the button's own. */
function ActionButton(
  { action, busy, phase, onFire }:
  { action: ActionSpec; busy: boolean; phase: string | null; onFire: () => void },
) {
  const [armed, setArmed] = useState(false)

  // Auto-disarm a risky confirm so a user who reconsiders isn't left with a
  // sticky "Confirm?" — mirrors the outcome-badge auto-clear (#466).
  useEffect(() => {
    if (!armed) return
    const id = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(id)
  }, [armed])

  function onClick() {
    if (action.style === 'risky' && !armed) { setArmed(true); return }
    setArmed(false)
    onFire()
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
    </span>
  )
}

function OutcomeBadge({ job }: { job: ItemJob | undefined }) {
  const outcome = job?.outcome
  if (!outcome) return null
  const badge = (
    <Badge tone={outcome.level === 'ok' ? 'success' : outcome.level === 'warn' ? 'warning' : 'danger'}>
      {outcome.message}
    </Badge>
  )
  if (!outcome.link) return badge
  // A plain anchor, not a router Link: this list also renders outside a Router.
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {badge}
      <a href={outcome.link.href} className="text-sm text-accent underline">{outcome.link.label}</a>
    </span>
  )
}

/** Rows the Overview preview shows before handing off to the Needs you tab
 *  (docs/PAGE-TABS.md §5: a preview is at most three rows + "See all N"). */
export const PREVIEW_ROWS = 3

export function AutomationActions({ automationId, enabled, variant, paused = false, onSeeAll }: {
  automationId: string
  enabled: boolean
  /** The automation is paused (`Automation.paused`): its decisions are parked —
   *  still listed with working buttons, headed "Paused — N parked" so the
   *  operator knows why the Delivery badge left them out (#2936). */
  paused?: boolean
  /** `preview`: the Overview's notice — heading, three rows, "See all N",
   *  nothing at all when empty. `full`: the Needs you tab — every row on the
   *  page scroll, a sentence when empty. */
  variant: 'preview' | 'full'
  /** Preview only: switch to the tab that owns the full list. */
  onSeeAll?: () => void
}) {
  const actionable = useAutomationActionable(automationId, enabled)
  // One job map for every row this list renders, so a result outlives its
  // row's button and follows the item between the decisions and the fold.
  const jobs = useActionJobs(automationId)
  // Decisions are what "needs you"; maintenance (an Unmute on a muted
  // signature, a File-now on a row the loop itself throttled) is settled
  // housekeeping — said, folded, never counted (2026-10-01 Delivery critique, P0).
  const { decisions, maintenance } = splitActionable(actionable.data ?? [])
  const parkedHeading = paused && decisions.length > 0 ? `Paused — ${decisions.length} parked` : null
  if (variant === 'preview') {
    if (decisions.length === 0) return null
    return (
      <section className="space-y-2">
        <SectionHeading>{parkedHeading ?? 'Actionable items'}</SectionHeading>
        <div className="rounded-md border border-edge p-2">
          <ActionableRows items={decisions.slice(0, PREVIEW_ROWS)} jobs={jobs} />
        </div>
        {onSeeAll && decisions.length > PREVIEW_ROWS && (
          <div>
            <Button size="sm" variant="ghost" onClick={onSeeAll}>See all {decisions.length}</Button>
          </div>
        )}
      </section>
    )
  }
  return (
    <div className="space-y-3">
      {parkedHeading && <ParkedNotice heading={parkedHeading} />}
      {decisions.length === 0
        ? <p className="text-sm text-ink-muted">Nothing needs you.</p>
        : <ActionableRows items={decisions} jobs={jobs} />}
      {maintenance.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer select-none text-ink-muted">
            {maintenance.length} maintenance
          </summary>
          <div className="mt-2">
            <ActionableRows items={maintenance} jobs={jobs} />
          </div>
        </details>
      )}
    </div>
  )
}

/** The paused automation's "these are parked, not waiting on you" notice — one
 *  copy for the flat list and the clustered inbox (`FeedbackInbox`). */
export function ParkedNotice({ heading }: { heading: string }) {
  return (
    <div className="space-y-1">
      <SectionHeading>{heading}</SectionHeading>
      <p className="max-w-prose text-sm text-ink-muted">
        Not counted in the Delivery badge while this automation is paused. You can still act on them here.
      </p>
    </div>
  )
}

function ActionableRows(
  { items, jobs: { jobs, run } }: { items: ActionableItem[]; jobs: ReturnType<typeof useActionJobs> },
) {
  return (
    <ul className="space-y-1.5 text-sm">
      {items.map((item) => {
        const number = item.id.split('#')[1]
        const job = jobs[item.id]
        return (
          <li key={item.id} className="flex items-center gap-2">
            <span className="min-w-0 flex-1">
              <span className="block truncate">
                {number && <span className="text-ink-muted tabular-nums">#{number}</span>}{number ? ' ' : ''}
                {item.title}
              </span>
              {item.subtitle && <span className="block truncate text-xs text-ink-muted">{item.subtitle}</span>}
            </span>
            {item.actions.length === 0 && item.status && (
              <span className="text-xs text-ink-muted">{item.status}</span>
            )}
            {item.actions.map((action) => (
              <ActionButton
                key={action.kind}
                action={action}
                busy={job?.busy ?? false}
                phase={job?.phase ?? null}
                onFire={() => void run(item, action)}
              />
            ))}
            <OutcomeBadge job={job} />
          </li>
        )
      })}
    </ul>
  )
}
