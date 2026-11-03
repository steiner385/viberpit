import { useState } from 'react'
import { cn } from '@/lib/utils'
import type { Turn } from '@/session/reducer'
import { rewindTargetFor } from '@/session/rewindTarget'
import { useRewindActions } from '@/features/sessions/rewind/rewindContext'

interface Props {
  code: string
  message: string
  resetsAt?: string | null
  /** The user prompt whose turn failed (`failedPrompts`) — what Rewind & retry rewinds to. */
  prompt?: Turn | null
}

/**
 * Inline transcript card for an error anchored to a turn. Mirrors the top-level
 * error banner's styling but lives in the message timeline, so the failure stays
 * visible in place even after the (single-slot) banner is replaced by a later error.
 *
 * Rewind & retry (D-9): in a live session that can rewind, while the server would not
 * refuse (`busy`), and when the failed prompt has a rewind target — confirm, rewind to
 * that prompt, then send it again (`RewindActions.retry`).
 */
export function ErrorCard({ code, message, resetsAt, prompt }: Props) {
  const rewind = useRewindActions()
  const [retrying, setRetrying] = useState(false)
  const canRetry = rewind !== null && !rewind.busy && prompt != null && rewindTargetFor(prompt) !== null
  return (
    <div
      role="alert"
      className={cn(
        'my-1 rounded-md px-3 py-2 text-sm',
        'bg-danger-wash text-danger',
        'border border-danger',
      )}
    >
      <span className="font-medium">Error</span>
      <span className="ml-1 font-mono text-xs opacity-75">({code})</span>
      <div className="mt-0.5 whitespace-pre-wrap break-words">{message}</div>
      {resetsAt && (
        <div className="mt-1 text-xs opacity-75">
          Resets at {new Date(resetsAt).toLocaleTimeString()}
        </div>
      )}
      {canRetry && (
        <div className="mt-1.5 flex justify-end">
          <button
            type="button"
            disabled={retrying}
            onClick={() => { setRetrying(true); void rewind.retry(prompt).finally(() => setRetrying(false)) }}
            className={cn(
              'rounded px-1.5 py-0.5 text-xs font-medium text-danger',
              'hover:bg-danger/10 disabled:opacity-50',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            )}
          >
            Rewind &amp; retry
          </button>
        </div>
      )}
    </div>
  )
}
