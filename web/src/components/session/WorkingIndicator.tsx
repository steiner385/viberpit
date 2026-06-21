import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { pickWorkingWord } from './workingWords'

/** Returns true when the user has requested reduced motion. Safe in jsdom (matchMedia may be absent). */
function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

interface Props {
  /** Number of queued prompts (shows "· N queued" when > 0). */
  count?: number
  className?: string
}

/**
 * Animated "working" indicator — a CSS spinner + a randomly-rotating whimsical
 * gerund + an animated trailing ellipsis.
 *
 * Accessibility:
 *   - The animated visuals are aria-hidden; an sr-only element carries the
 *     stable announcement ("Working…" / "Working, N queued") for screen readers.
 *   - When prefers-reduced-motion is set: no interval rotation, no spin animation.
 */
export function WorkingIndicator({ count = 0, className }: Props) {
  const reducedMotion = prefersReducedMotion()

  // Pick an initial word on mount; stable ref so the initial value only runs once.
  const [word, setWord] = useState<string>(() => pickWorkingWord())
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (reducedMotion) return

    intervalRef.current = setInterval(() => {
      setWord(pickWorkingWord())
    }, 2800)

    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [reducedMotion])

  const srLabel = count > 0 ? `Working, ${count} queued` : 'Working…'

  return (
    <span className={cn('inline-flex items-center gap-1.5', className)}>
      {/* Visually-hidden stable label for screen readers */}
      <span className="sr-only">{srLabel}</span>

      {/* Animated visuals — hidden from AT */}
      <span aria-hidden="true" className="inline-flex items-center gap-1.5">
        {/* Spinner glyph */}
        <span
          className={cn(
            'inline-block text-current opacity-70',
            !reducedMotion && 'animate-spin',
          )}
          style={{ animationDuration: '1s' }}
        >
          ◌
        </span>

        {/* Rotating gerund */}
        <span>{word}</span>

        {/* Animated trailing ellipsis */}
        {reducedMotion ? (
          <span>…</span>
        ) : (
          <span className="inline-flex">
            <span className="animate-[blink_1.2s_step-end_0.0s_infinite]">.</span>
            <span className="animate-[blink_1.2s_step-end_0.4s_infinite]">.</span>
            <span className="animate-[blink_1.2s_step-end_0.8s_infinite]">.</span>
          </span>
        )}

        {/* Optional queued count */}
        {count > 0 && (
          <span className="opacity-60">· {count} queued</span>
        )}
      </span>
    </span>
  )
}

