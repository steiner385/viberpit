import { cn } from '@/lib/utils'

export interface GanttBarProps {
  fillPct: number
  colorClass: string
  bandLeftPct?: number | null
  bandWidthPct?: number | null
  tickPct?: number | null
  textureImage?: string
  ariaNow?: number
  testId?: string
  className?: string
}

// Single shared bar primitive: track + optional p10–p90 band + fill (with optional
// CVD-safe texture overlay) + optional nominal tick. Used by CheckRowList and StageTrack.
export function GanttBar({
  fillPct, colorClass, bandLeftPct, bandWidthPct, tickPct, textureImage, ariaNow, testId,
  className = 'h-1.5 flex-1',
}: GanttBarProps) {
  return (
    <span
      data-testid={testId}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(ariaNow ?? fillPct)}
      className={cn('relative block overflow-hidden rounded bg-gray-200 dark:bg-gray-700', className)}
    >
      {bandLeftPct != null && bandWidthPct != null && bandWidthPct > 0 && (
        <span data-band="range" className={cn('absolute inset-y-0 opacity-30', colorClass)}
          style={{ left: `${bandLeftPct}%`, width: `${bandWidthPct}%` }} aria-hidden />
      )}
      <span data-band="solid" className={cn('absolute inset-y-0 left-0 rounded-l', colorClass)}
        style={{ width: `${fillPct}%` }} aria-hidden>
        {textureImage ? (
          <span data-band="texture" className="absolute inset-0" style={{ backgroundImage: textureImage }} aria-hidden />
        ) : null}
      </span>
      {tickPct != null && (
        <span data-band="tick" className="absolute inset-y-[-1px] w-px bg-foreground"
          style={{ left: `${tickPct}%` }} aria-hidden />
      )}
    </span>
  )
}
