// web/src/components/StageTiles.tsx
import { cn } from '@/lib/utils'
import {
  STAGE_BUCKETS, STAGE_BUCKET_LABELS, stageBucketOf, type StageBucket,
} from '@/session/stageBucket'
import { anyChecksFailing } from '@/session/prStatus'
import type { PipelinePr, StageModel } from '@/api/types'

export function StageTiles({
  prs, stageModels, active, onToggle, failedActive = false, onToggleFailed,
}: {
  prs: PipelinePr[]
  stageModels: Record<string, StageModel>
  active: Set<StageBucket>
  onToggle: (b: StageBucket) => void
  failedActive?: boolean
  onToggleFailed?: () => void
}) {
  const counts: Record<StageBucket, number> = { ci: 0, ready: 0, queue: 0, deploy: 0, merged: 0 }
  for (const p of prs) {
    const b = stageBucketOf(p, stageModels[p.repo ?? ''])
    if (b) counts[b] += 1
  }
  // "Failed" is a status facet, not a stage — count via the SAME predicate that
  // drives the red ✗ and the triage bucket, so a closed/merged PR's frozen
  // failing rollup never leaks in.
  const failedCount = prs.reduce((n, p) => n + (anyChecksFailing([p]) ? 1 : 0), 0)
  const failedDisabled = failedCount === 0
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter PRs">
      {STAGE_BUCKETS.map((b) => {
        const n = counts[b]
        const on = active.has(b)
        const disabled = n === 0
        return (
          <button
            key={b}
            type="button"
            data-testid={`stage-tile-${b}`}
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onToggle(b)}
            className={cn(
              'flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs',
              disabled && 'opacity-40 cursor-default',
              on
                ? 'border-accent bg-accent/15 text-accent-foreground ring-2 ring-accent/40'
                : 'border-gray-200 dark:border-gray-700 hover:bg-muted/60',
            )}
          >
            <span className="font-medium uppercase tracking-wide text-[10px]">
              {STAGE_BUCKET_LABELS[b]}
            </span>
            <span className="tabular-nums">{n}</span>
          </button>
        )
      })}
      <span className="mx-0.5 h-5 w-px bg-gray-200 dark:bg-gray-700" aria-hidden />
      <button
        type="button"
        data-testid="pr-filter-failed"
        aria-pressed={failedActive}
        disabled={failedDisabled}
        onClick={() => onToggleFailed?.()}
        className={cn(
          'flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs',
          failedDisabled && 'opacity-40 cursor-default',
          failedActive
            ? 'border-red-400 bg-red-500/15 text-red-700 ring-2 ring-red-400/40 dark:text-red-300'
            : 'border-gray-200 dark:border-gray-700 hover:bg-muted/60',
        )}
      >
        <span className="font-medium uppercase tracking-wide text-[10px]">Failed</span>
        <span className="tabular-nums">{failedCount}</span>
      </button>
    </div>
  )
}
