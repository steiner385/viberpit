/**
 * SessionPlaceholder — what fills the transcript area when a session has no turns
 * to display yet. Replaces the previously-blank pane with an explicit reason, so
 * the user can tell apart the three states that used to look identical:
 *
 *   • connecting / reconnecting  — the socket isn't live yet
 *   • loading                    — connected, replaying history (turns incoming)
 *   • empty                      — replay finished with nothing to show
 *
 * Plus an error hand-off (the error banner above owns the message) and a
 * "taking longer than usual" hint if a spinner phase drags on, which is the
 * client-side signal that a load may be stuck rather than merely slow.
 */
import { useState, useEffect } from 'react'
import { cn } from '@/lib/utils'
import type { ConnectionState } from '@/ws/client'
import { useBrailleSpinner } from './brailleSpinner'

interface Props {
  connectionState: ConnectionState
  /** True until the server's replayComplete event (history still streaming in). */
  replaying: boolean
  /** True when an error frame has populated the error banner above. */
  hasError: boolean
  /** Re-attach the session. When provided, a hard-timeout "stuck" state offers a
   *  Retry button so a load that never completes is recoverable without a full
   *  page refresh. */
  onRetry?: () => void
}

type Phase = 'connecting' | 'loading' | 'error' | 'empty'

/** Resolve which placeholder to show. Connection problems outrank loading; a
 *  live error outranks the empty state. */
function resolvePhase({ connectionState, replaying, hasError }: Props): Phase {
  if (connectionState !== 'connected') return 'connecting'
  if (replaying) return 'loading'
  if (hasError) return 'error'
  return 'empty'
}

/** How long a spinner phase may run before we hint that it might be stuck. */
const SLOW_AFTER_MS = 8_000
/** Hard ceiling: past this, a spinner phase is treated as stuck and we stop
 *  spinning, surfacing a recoverable error instead of an endless loader. */
const STUCK_AFTER_MS = 45_000

export function SessionPlaceholder(props: Props) {
  const phase = resolvePhase(props)
  const isSpinner = phase === 'connecting' || phase === 'loading'
  const glyph = useBrailleSpinner()

  // Spinner-phase timers: a gentle "taking a while" hint, then a hard "stuck"
  // cutoff. retryNonce re-arms both timers when the user retries.
  const [slow, setSlow] = useState(false)
  const [stuck, setStuck] = useState(false)
  const [retryNonce, setRetryNonce] = useState(0)
  useEffect(() => {
    if (!isSpinner) return
    const slowId = setTimeout(() => setSlow(true), SLOW_AFTER_MS)
    const stuckId = setTimeout(() => setStuck(true), STUCK_AFTER_MS)
    // Cleanup fires when the spinner phase ends (load completed) or on re-arm:
    // clear the pending timers and drop any stale slow/stuck flags so a later
    // reconnect doesn't immediately flash the "didn't load" error.
    return () => {
      clearTimeout(slowId)
      clearTimeout(stuckId)
      setSlow(false)
      setStuck(false)
    }
  }, [isSpinner, retryNonce])

  const handleRetry = () => {
    // Reset to the spinner and re-arm the timers, then re-attach.
    setSlow(false)
    setStuck(false)
    setRetryNonce((n) => n + 1)
    props.onRetry?.()
  }

  // A stuck spinner outranks the spinner itself: we stop pretending to load.
  const showStuck = isSpinner && stuck

  const label =
    phase === 'connecting'
      ? props.connectionState === 'connecting'
        ? 'Connecting…'
        : 'Reconnecting…'
      : phase === 'loading'
        ? 'Loading session…'
        : null

  return (
    <div
      data-testid="session-placeholder"
      data-phase={showStuck ? 'stuck' : phase}
      className="flex-1 flex flex-col items-center justify-center px-6 py-10 text-center"
    >
      {showStuck && (
        <div
          data-testid="session-placeholder-stuck"
          role="alert"
          className="flex flex-col items-center gap-2 text-gray-500 dark:text-gray-400"
        >
          <p className="text-sm font-medium">This session didn’t load</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 max-w-xs">
            It’s taking much longer than expected — the server may be busy or the
            connection stuck.
          </p>
          {props.onRetry && (
            <button
              type="button"
              onClick={handleRetry}
              className={cn(
                'mt-1 rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
                'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
            >
              Retry
            </button>
          )}
        </div>
      )}

      {isSpinner && !showStuck && (
        <div
          // aria-live (not role="status") so this announces without colliding
          // with the StatusBar's status region in the same view.
          aria-live="polite"
          aria-busy="true"
          className="flex flex-col items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
        >
          <span className="inline-flex items-center gap-2">
            <span aria-hidden="true" className="text-lg opacity-70 tabular-nums">
              {glyph}
            </span>
            <span>{label}</span>
          </span>
          {slow && (
            <span
              data-testid="session-placeholder-slow"
              className="text-xs text-gray-400 dark:text-gray-500"
            >
              Taking longer than usual — the connection may be slow or stuck.
            </span>
          )}
        </div>
      )}

      {phase === 'empty' && (
        <div className="flex flex-col items-center gap-1 text-gray-500 dark:text-gray-400">
          <p className="text-sm font-medium">No messages yet</p>
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Send a message below to start this session.
          </p>
        </div>
      )}

      {phase === 'error' && (
        <p className={cn('text-sm text-gray-500 dark:text-gray-400')}>
          This session couldn’t be loaded — see the error above.
        </p>
      )}
    </div>
  )
}
