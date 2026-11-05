// One gate condition (spec 2026-11-05 §9). Line 1: the status glyph in a fixed
// narrow column, then the label at full width so it reads at a glance. Line 2:
// the status text, the value against its target, its age, and a deep link.
// Status is glyph + text, never colour alone; a failed reading's error rides a
// tooltip and `aria-describedby`. A manual condition, when the row is managed,
// offers Tick (or Update once ticked), which opens the tick dialog.
import { useId, useState } from 'react'
import type { MilestoneCondition } from '@/api/milestones'
import { TONE_TEXT } from '@/ui/Badge'
import { cn } from '@/lib/utils'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { Button } from '@/ui/Button'
import { STATUS_META, conditionValue } from './format'
import { TickDialog } from './TickDialog'

/** Where a row's tick goes: the product and the milestone key (or `NUMBERS_SCOPE`). */
export interface TickTarget { slug: string; scope: string }

/** "Tick" until someone has ticked it, then "Update". */
const tickVerb = (c: MilestoneCondition) => (c.manualBy ? 'Update' : 'Tick')

/** The Tick / Update button and its dialog, for a manual condition. */
export function TickControl({ condition: c, target }: { condition: MilestoneCondition; target: TickTarget }) {
  const [open, setOpen] = useState(false)
  const verb = tickVerb(c)
  return (
    <>
      <Button type="button" size="sm" variant="secondary" className="h-7 shrink-0 px-2 text-xs"
        aria-label={`${verb}: ${c.label}`} onClick={() => setOpen(true)}>
        {verb}
      </Button>
      {open && <TickDialog open onClose={() => setOpen(false)} slug={target.slug} scope={target.scope} condition={c} />}
    </>
  )
}

export function ConditionRow({ condition: c, manage }: { condition: MilestoneCondition; manage?: TickTarget }) {
  const errorId = useId()
  const meta = STATUS_META[c.status] ?? STATUS_META.unknown
  const value = conditionValue(c)
  return (
    <li data-status={c.status} className="flex flex-col gap-0.5 py-1.5 text-sm">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className={cn('w-3 shrink-0 text-center text-xs font-medium leading-5', TONE_TEXT[meta.tone])}>
          {meta.glyph}
        </span>
        <span className="min-w-0 flex-1 text-ink">{c.label}</span>
        {manage && c.kind === 'manual' && <TickControl condition={c} target={manage} />}
      </div>
      <div className="flex flex-wrap gap-x-2 pl-5 text-xs text-ink-muted">
        <span
          title={c.error ?? undefined}
          aria-describedby={c.error ? errorId : undefined}
          className="font-medium text-ink"
        >
          {meta.text}
        </span>
        {value && <span className="tabular-nums text-ink">{value}</span>}
        <span>{isValidTimestamp(c.measuredAt) ? relTime(c.measuredAt) : 'Never measured'}</span>
        {isSafeHttpUrl(c.sourceUrl) && (
          <a href={c.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            source ↗
          </a>
        )}
        {c.manualBy && (
          <span className="[overflow-wrap:anywhere]">
            by {c.manualBy}{c.manualEvidence ? ` · ${c.manualEvidence}` : ''}
            {/* Only an absolute http(s) link becomes an href (stored-XSS guard). */}
            {isSafeHttpUrl(c.manualUrl) && (
              <>{' · '}<a href={c.manualUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">evidence</a></>
            )}
          </span>
        )}
      </div>
      {c.error && <span id={errorId} className="sr-only">{c.error}</span>}
    </li>
  )
}
