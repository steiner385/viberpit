import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { pickWorkingWord } from './workingWords'
import { formatElapsed } from './elapsed'

/** Returns true when the user has requested reduced motion. Safe in jsdom (matchMedia may be absent). */
function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Braille spinner frames — the classic Claude-Code-style cycling dot glyph. */
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] as const
const SPINNER_INTERVAL_MS = 80
/** Static glyph shown instead of the cycling spinner under reduced-motion. */
const STATIC_SPINNER = '⠿'

/** Module-level memory of the last phrase shown, so consecutive waits never repeat it. */
let lastWord: string | undefined

interface Props {
  /** Number of queued prompts (shows "· N queued" when > 0). */
  count?: number
  className?: string
}

/**
 * Animated "working" indicator — a cycling braille spinner + a whimsical gerund
 * (shimmering) + a live elapsed-time readout, Claude-Code style.
 *
 * Phrase lifecycle: a word is chosen once when this component mounts (i.e. when a
 * new working period begins) and stays fixed for the whole period. Because the
 * indicator is only mounted while status === 'working', each new "waiting for
 * Claude" instance naturally gets a fresh phrase — and never the same one twice
 * in a row (see module-level `lastWord`). There is no periodic phrase rotation.
 *
 * Accessibility:
 *   - The animated visuals are aria-hidden; an sr-only element carries the stable
 *     announcement ("Working…" / "Working, N queued") for screen readers. The
 *     elapsed readout is visual-only so AT doesn't announce every tick.
 *   - prefers-reduced-motion: static spinner glyph, no shimmer, no spinner frames.
 */
export function WorkingIndicator({ count = 0, className }: Props) {
  const reducedMotion = prefersReducedMotion()

  // Pick the phrase once on mount; remember it so the next wait won't repeat it.
  const [word] = useState<string>(() => {
    const next = pickWorkingWord(lastWord)
    lastWord = next
    return next
  })

  // Cycling braille spinner frame index (motion only).
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    if (reducedMotion) return
    const id = setInterval(() => {
      setFrame((f) => (f + 1) % SPINNER_FRAMES.length)
    }, SPINNER_INTERVAL_MS)
    return () => clearInterval(id)
  }, [reducedMotion])

  // Live elapsed time since this working period began. The start instant is
  // captured in the mount effect (not during render — Date.now() is impure) and
  // never re-read, so re-renders don't drift; the readout ticks once per second.
  const startRef = useRef<number>(0)
  const [elapsedMs, setElapsedMs] = useState(0)
  useEffect(() => {
    startRef.current = Date.now()
    const id = setInterval(() => {
      setElapsedMs(Date.now() - startRef.current)
    }, 1000)
    return () => clearInterval(id)
  }, [])

  const srLabel = count > 0 ? `Working, ${count} queued` : 'Working…'
  const spinnerGlyph = reducedMotion ? STATIC_SPINNER : SPINNER_FRAMES[frame]

  return (
    <span className={cn('inline-flex items-center gap-1.5', className)}>
      {/* Visually-hidden stable label for screen readers */}
      <span className="sr-only">{srLabel}</span>

      {/* Animated visuals — hidden from AT */}
      <span aria-hidden="true" className="inline-flex items-center gap-1.5">
        {/* Braille spinner glyph */}
        <span data-testid="working-spinner" className="inline-block text-current opacity-70 tabular-nums">
          {spinnerGlyph}
        </span>

        {/* Phrase + ellipsis (shimmers unless reduced-motion) */}
        <span className={cn(!reducedMotion && 'working-shimmer')}>{word}…</span>

        {/* Live elapsed-time readout */}
        <span data-testid="working-elapsed" className="opacity-60 tabular-nums">
          · {formatElapsed(elapsedMs)}
        </span>

        {/* Optional queued count */}
        {count > 0 && <span className="opacity-60">· {count} queued</span>}
      </span>
    </span>
  )
}
