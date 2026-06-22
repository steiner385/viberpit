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

export function SessionPlaceholder(props: Props) {
  const phase = resolvePhase(props)
  const isSpinner = phase === 'connecting' || phase === 'loading'
  const glyph = useBrailleSpinner()

  // After a while in a spinner phase, surface a gentle "this is taking a while"
  // hint — the difference between "still loading" and "probably stuck".
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    if (!isSpinner) return
    const id = setTimeout(() => setSlow(true), SLOW_AFTER_MS)
    return () => clearTimeout(id)
  }, [isSpinner])

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
      data-phase={phase}
      className="flex-1 flex flex-col items-center justify-center px-6 py-10 text-center"
    >
      {isSpinner && (
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
