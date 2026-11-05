// A milestone's leaves (spec 2026-11-05 §9): done / in flight / not started as
// proportional segments plus the counts in words. The bar is decoration; the
// sentence carries the numbers for a screen reader.
import type { MilestoneLeaves } from '@/api/milestones'
import { leavesLine } from './format'

const SEGMENTS = [
  { key: 'done', className: 'bg-success' },
  { key: 'inFlight', className: 'bg-info' },
  { key: 'notStarted', className: 'bg-neutral-wash' },
] as const

export function LeavesBar({ leaves }: { leaves: MilestoneLeaves }) {
  return (
    <div className="flex flex-col gap-1">
      <div data-leaves-bar aria-hidden="true" className="flex h-1.5 overflow-hidden rounded-pill bg-surface-raised">
        {leaves.total > 0 && SEGMENTS.map((s) => (
          <span
            key={s.key}
            data-segment={s.key === 'inFlight' ? 'in_flight' : s.key === 'notStarted' ? 'not_started' : 'done'}
            className={s.className}
            style={{ width: `${(leaves[s.key] / leaves.total) * 100}%` }}
          />
        ))}
      </div>
      <p className="text-xs text-ink-muted tabular-nums">{leavesLine(leaves)}</p>
    </div>
  )
}
