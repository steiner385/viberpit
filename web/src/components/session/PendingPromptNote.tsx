import { cn } from '@/lib/utils'
import { type PromptStage, STAGE_LABEL, isInFlight } from '@/session/promptStage'
import { useBrailleSpinner, prefersReducedMotion } from './brailleSpinner'

interface Props { stage: PromptStage; className?: string }

/**
 * The one-line status note under a sent-but-unconfirmed prompt bubble.
 *
 * Sits in the window between "the user hit Enter" and "the agent's turn started" —
 * previously a completely blank transcript for as long as 2 minutes. The label
 * deliberately names the SERVER-side step ("waking session…") rather than a generic
 * spinner: the wait is not shorter, but a named wait is a legible one, and it tells
 * the user whether to keep waiting or to suspect something broke.
 *
 * Reuses the braille spinner the WorkingIndicator uses, so the pre-turn wait and the
 * turn itself read as one continuous motion rather than two different vocabularies.
 */
export function PendingPromptNote({ stage, className }: Props) {
  const spinnerGlyph = useBrailleSpinner()
  const inFlight = isInFlight(stage)
  const label = STAGE_LABEL[stage]

  return (
    <div className={cn('mt-1 flex justify-end', className)}>
      <div
        role="status"
        aria-live="polite"
        className={cn(
          'inline-flex items-center gap-1.5 text-xs tabular-nums',
          inFlight ? 'text-gray-400 dark:text-gray-500' : 'text-warning',
        )}
      >
        {inFlight && (
          <span
            data-testid="pending-prompt-spinner"
            aria-hidden="true"
            className={cn('inline-block opacity-70', !prefersReducedMotion() && 'working-shimmer')}
          >
            {spinnerGlyph}
          </span>
        )}
        <span>{label}</span>
      </div>
    </div>
  )
}
