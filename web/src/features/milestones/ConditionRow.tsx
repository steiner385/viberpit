// One gate condition (spec 2026-11-05 §9): status as glyph + text, the label,
// the value against its target, its age, and a deep link. A failed reading's
// error rides a tooltip and `aria-describedby`, so it is never colour alone.
import { useId } from 'react'
import type { MilestoneCondition } from '@/api/milestones'
import { TONE_TEXT } from '@/ui/Badge'
import { cn } from '@/lib/utils'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { STATUS_META, conditionValue } from './format'

export function ConditionRow({ condition: c }: { condition: MilestoneCondition }) {
  const errorId = useId()
  const meta = STATUS_META[c.status] ?? STATUS_META.unknown
  const value = conditionValue(c)
  return (
    <li className="flex flex-col gap-0.5 py-1.5 text-sm">
      <div className="flex items-start gap-2">
        <span
          data-status={c.status}
          title={c.error ?? undefined}
          aria-describedby={c.error ? errorId : undefined}
          className="shrink-0 whitespace-nowrap text-xs font-medium text-ink"
        >
          <span aria-hidden="true" className={cn('inline-block w-3 text-center', TONE_TEXT[meta.tone])}>{meta.glyph}</span>
          {' '}<span>{meta.text}</span>
        </span>
        <span className="min-w-0 flex-1 text-ink">{c.label}</span>
      </div>
      <div className="flex flex-wrap gap-x-2 pl-5 text-xs text-ink-muted">
        {value && <span className="tabular-nums text-ink">{value}</span>}
        <span>{isValidTimestamp(c.measuredAt) ? relTime(c.measuredAt) : 'Never measured'}</span>
        {c.sourceUrl && (
          <a href={c.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            source ↗
          </a>
        )}
        {c.manualBy && (
          <span>Ticked by {c.manualBy}{c.manualEvidence ? ` · ${c.manualEvidence}` : ''}</span>
        )}
      </div>
      {c.error && <span id={errorId} className="sr-only">{c.error}</span>}
    </li>
  )
}
