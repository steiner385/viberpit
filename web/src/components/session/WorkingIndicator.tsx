import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { pickWorkingWord } from './workingWords'
import { formatElapsed } from './elapsed'
import { useBrailleSpinner, prefersReducedMotion } from './brailleSpinner'

/** Module-level memory of the last phrase shown, so consecutive waits never repeat it. */
let lastWord: string | undefined

interface Props {
  /** Number of queued prompts (shows "· N queued" when > 0). */
  count?: number
  /** Number of running sub-agents. When > 0, overrides the whimsical phrase
   *  with "Running a sub-agent…" / "Running N sub-agents…". */
  subAgentCount?: number
  /** Optional phrase override. Replaces the whimsical word but yields to
   *  `subAgentCount` (sub-agent phrase still wins when both are present). */
  phrase?: string
  className?: string
}

/**
 * Animated "working" indicator — a cycling braille spinner + a whimsical gerund
 * (shimmering) + a live elapsed-time readout, Claude-Code style.
 *
 * Phrase lifecycle: a word is chosen once when this component mounts (i.e. when a
 * new working period begins) and stays fixed for the whole period. Because the
 * indicator is only mounted while the session is busy, each new "waiting for
 * Claude" instance naturally gets a fresh phrase — and never the same one twice
 * in a row (see module-level `lastWord`). There is no periodic phrase rotation.
 *
 * When `subAgentCount > 0`, the phrase is replaced with a more informative
 * "Running a sub-agent…" / "Running N sub-agents…" label so the user can tell
 * a background sub-agent is what's currently in flight.
 *
 * Accessibility:
 *   - The animated visuals are aria-hidden; an sr-only element carries the stable
 *     announcement ("Working…" / "Working, N queued" / "Running a sub-agent…")
 *     for screen readers. The elapsed readout is visual-only so AT doesn't
 *     announce every tick.
 *   - prefers-reduced-motion: static spinner glyph, no shimmer, no spinner frames.
 */
export function WorkingIndicator({ count = 0, subAgentCount = 0, phrase, className }: Props) {
  const reducedMotion = prefersReducedMotion()

  // Pick the phrase once on mount; remember it so the next wait won't repeat it.
  const [word] = useState<string>(() => {
    const next = pickWorkingWord(lastWord)
    lastWord = next
    return next
  })

  // Cycling braille spinner glyph (shared with SessionPlaceholder).
  const spinnerGlyph = useBrailleSpinner()

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

  // A running sub-agent is the more informative thing to surface.
  const subAgentPhrase = subAgentCount > 0
    ? (subAgentCount === 1 ? 'Running a sub-agent' : `Running ${subAgentCount} sub-agents`)
    : null
  const phraseText = subAgentPhrase ?? phrase ?? word
  const srLabel = subAgentPhrase
    ? `${subAgentPhrase}…`
    : phrase
      ? `${phrase}…`
      : (count > 0 ? `Working, ${count} queued` : 'Working…')

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
        <span className={cn(!reducedMotion && 'working-shimmer')}>{phraseText}…</span>

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
