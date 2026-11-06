import { lazy, Suspense } from 'react'
import { cn } from '@/lib/utils'
import type { ErrorKind } from '@/session/events'
import type { Turn } from '@/session/reducer'
import { retryablePrompt } from '@/session/rewindTarget'
import { useRewindActions } from '@/features/sessions/rewind/rewindContext'
import { ERROR_ACTION_CLASS, useErrorRecovery } from './errorRecoveryContext'

/** Switch model / Lower effort / Compact (D-19): only a failed turn loads them. */
const ErrorRecoveryActions = lazy(() => import('./ErrorRecoveryActions'))

interface Props {
  code: string
  message: string
  resetsAt?: string | null
  /** The server's classification (D-19); absent = `other`. */
  errorKind?: ErrorKind
  /** The user prompt whose turn failed (`failedPrompts`) — what Rewind & retry rewinds to. */
  prompt?: Turn | null
}

/**
 * Inline transcript card for an error anchored to a turn. Mirrors the top-level
 * error banner's styling but lives in the message timeline, so the failure stays
 * visible in place even after the (single-slot) banner is replaced by a later error.
 *
 * Rewind & retry (D-9): on the latest prompt's own failure (`failedPrompts`), in a live
 * session that can rewind, while the server would not refuse (`busy`), and when the
 * prompt can be resent as typed (`retryablePrompt`) — confirm, rewind to that prompt,
 * then send it again (`RewindActions.retry`). Disabled while any rewind is in hand.
 * Not for `model_unavailable`: resending to the same missing model fails the same way.
 *
 * The rest by `errorKind` (D-19, `ErrorRecoveryActions`), in a live session only
 * (`ErrorRecoveryContext`): Switch model and Lower effort for a rate limit or an
 * overload, Switch model alone for a missing model, Compact for a context overflow.
 */
export function ErrorCard({ code, message, resetsAt, errorKind = 'other', prompt }: Props) {
  const rewind = useRewindActions()
  const recovery = useErrorRecovery()
  const canRetry = errorKind !== 'model_unavailable'
    && rewind !== null && !rewind.busy && prompt != null && retryablePrompt(prompt)
  const more = recovery !== null && errorKind !== 'other'
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
      {(canRetry || more) && (
        <div className="mt-1.5 flex flex-wrap justify-end gap-1">
          {more && (
            <Suspense fallback={null}>
              <ErrorRecoveryActions kind={errorKind} recovery={recovery} className={ERROR_ACTION_CLASS} />
            </Suspense>
          )}
          {canRetry && (
            <button
              type="button"
              disabled={rewind.open}
              onClick={() => { void rewind.retry(prompt) }}
              className={ERROR_ACTION_CLASS}
            >
              Rewind &amp; retry
            </button>
          )}
        </div>
      )}
    </div>
  )
}
