// The side panel's Numbers (spec 2026-11-05 §9): context figures with no gate,
// each a value, its label and its age. Needs-you and Decisions join in Phase 2.
import type { MilestoneCondition } from '@/api/milestones'
import { SectionHeading } from '@/ui/SectionHeading'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { conditionValue } from './format'

export function NumbersPanel({ numbers }: { numbers: MilestoneCondition[] }) {
  if (numbers.length === 0) return null
  return (
    <section aria-labelledby="milestones-numbers" className="flex flex-col gap-2">
      <SectionHeading id="milestones-numbers">Numbers</SectionHeading>
      {/* Stacked under the ladder below 2xl, the figures sit in a row; beside it, a column. */}
      <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-1">
        {numbers.map((n) => (
          // dt before dd for a valid group; the figure is lifted visually first.
          <div key={n.key} className="flex flex-col rounded-md border border-edge bg-surface p-3">
            <dt className="text-xs text-ink-muted">{n.label}</dt>
            <dd className="order-first text-xl font-semibold tabular-nums text-ink">{conditionValue(n, '') ?? '—'}</dd>
            <dd className="text-xs text-ink-faint">
              {isValidTimestamp(n.measuredAt) ? relTime(n.measuredAt) : 'Never measured'}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
