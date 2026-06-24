import type { ReactNode } from 'react'
import { formatBlockTime } from '@/lib/formatBlockTime'

/** Wraps a transcript block in a row with a fixed-width left time gutter. */
export function BlockRow({ ts, children }: { ts?: number; children: ReactNode }) {
  const { short, full } = formatBlockTime(ts)
  return (
    <div className="flex gap-2">
      <time className="w-10 shrink-0 select-none pt-1 text-right text-[10px] leading-5 tabular-nums text-muted-foreground" title={full || undefined}>
        {short}
      </time>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
