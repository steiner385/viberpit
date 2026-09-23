import { useState } from 'react'
import { ThumbsDown, ThumbsUp } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAssistantFeedback } from '@/api/assistant'
import { useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'

type Vote = 'up' | 'down'

interface Props {
  /** `turn.id` — already exists on every Turn, no new concept. */
  turnId: string
}

/**
 * Per-turn thumbs feedback for an assistant reply (spec v2 Task 10.1) — mounted
 * by `Turn` only for `variant === 'assistant'` assistant-role turns, i.e. only
 * inside the AI assistant drawer.
 *
 * `sessionId` is read from `SessionRuntimeProvider`'s context rather than
 * threaded down as a prop: `AssistantSheet` always wraps the drawer's
 * `TranscriptView` in a provider (`channel="assistant"`), so the context is
 * guaranteed present everywhere this component can mount. Reading it here —
 * instead of in `Turn`/`TranscriptView`, which render in plenty of contexts
 * that have no provider (e.g. their own isolated tests) — keeps every other
 * caller of those components unchanged.
 */
export function TurnFeedback({ turnId }: Props) {
  const { sessionId } = useSessionRuntimeContext()
  const feedback = useAssistantFeedback()
  const [voted, setVoted] = useState<Vote | null>(null)

  // Up/down are mutually exclusive; picking the other one switches the vote.
  // The backend contract has no "clear" vote (only up/down), so re-clicking the
  // already-pressed button is treated as a no-op rather than an undo — there is
  // nothing meaningful to send.
  function castVote(next: Vote) {
    if (voted === next) return
    setVoted(next)
    feedback.mutate({ sessionId, turnId, vote: next })
  }

  const btnClass = (pressed: boolean) => cn(
    'rounded p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
    pressed ? 'text-accent' : 'text-ink-faint hover:bg-surface-raised hover:text-ink-muted',
  )

  return (
    <div className="mt-1 flex items-center gap-1">
      <button
        type="button"
        aria-label="Helpful"
        aria-pressed={voted === 'up'}
        onClick={() => castVote('up')}
        className={btnClass(voted === 'up')}
      >
        <ThumbsUp size={14} aria-hidden="true" />
      </button>
      <button
        type="button"
        aria-label="Not helpful"
        aria-pressed={voted === 'down'}
        onClick={() => castVote('down')}
        className={btnClass(voted === 'down')}
      >
        <ThumbsDown size={14} aria-hidden="true" />
      </button>
    </div>
  )
}
