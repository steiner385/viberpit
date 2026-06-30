// web/src/components/StageTiles.tsx
import { cn } from '@/lib/utils'
import {
  STAGE_BUCKETS, STAGE_BUCKET_LABELS, stageBucketOf, type StageBucket,
} from '@/session/stageBucket'
import type { PipelinePr, StageModel } from '@/api/types'

export function StageTiles({
  prs, stageModels, active, onToggle,
}: {
  prs: PipelinePr[]
  stageModels: Record<string, StageModel>
  active: Set<StageBucket>
  onToggle: (b: StageBucket) => void
}) {
  const counts: Record<StageBucket, number> = { ci: 0, ready: 0, queue: 0, deploy: 0, merged: 0 }
  for (const p of prs) {
    const b = stageBucketOf(p, stageModels[p.repo ?? ''])
    if (b) counts[b] += 1
  }
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by stage">
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
    </div>
  )
}
