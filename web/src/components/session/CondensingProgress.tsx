import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { formatElapsed } from './elapsed'
import { prefersReducedMotion } from './brailleSpinner'

/**
 * Determinate-looking progress bar for context compaction. There is no real
 * compaction-progress signal, so this animates against a learned *expected*
 * duration: linear to 95% at the expected finish, then asymptotes (never reaching
 * 100%). The component unmounts when condensing ends — that is the only "done".
 */
export function CondensingProgress({ expectedMs }: { expectedMs: number }) {
  const reduced = prefersReducedMotion()
  const startRef = useRef<number>(0)
  const [elapsedMs, setElapsedMs] = useState(0)

  useEffect(() => {
    startRef.current = Date.now()
    const id = setInterval(() => setElapsedMs(Date.now() - startRef.current), reduced ? 1000 : 100)
    return () => clearInterval(id)
  }, [reduced])

  const frac = expectedMs > 0 ? elapsedMs / expectedMs : 1
  let pct: number
  if (frac < 1) {
    pct = Math.min(95, frac * 95)
  } else {
    const overrunSeconds = (elapsedMs - expectedMs) / 1000
    pct = 95 + 4 * (1 - 1 / (1 + overrunSeconds))   // → <99, never 100
  }
  const rounded = Math.round(pct)

  return (
    <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2 text-xs text-gray-400 dark:text-gray-500">
      <span className="font-medium">Condensing context…</span>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={rounded}
        aria-valuetext="Condensing context…"
        className="h-1.5 w-32 overflow-hidden rounded bg-gray-200 dark:bg-gray-700"
      >
        <div
          className={cn('h-full rounded bg-emerald-500', !reduced && 'transition-[width] duration-100 ease-linear')}
          style={{ width: `${rounded}%` }}
        />
      </div>
      <span className="tabular-nums opacity-70">{formatElapsed(elapsedMs)}</span>
    </div>
  )
}
