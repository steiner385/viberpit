/**
 * ReviewView — read-only transcript viewer for archived sessions.
 *
 * Fetches a bounded tail of the session transcript via useSessionTranscript,
 * folds the events through transcriptReducer to produce turns, and renders
 * them with TranscriptView in read-only mode (no composer, no WS connection).
 * Scrolling to the top pages in older turns (mirrors useSessionRuntime's
 * onLoadOlder, but self-contained — plain HTTP, no WS).
 */

import { useReducer, useEffect, useRef, useCallback } from 'react'
import { cn } from '@/lib/utils'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import { useSessionTranscript } from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { TranscriptView } from './session/TranscriptView'

/** Initial payload cap: the last N turns; older history pages in on scroll-up. */
const INITIAL_TURN_LIMIT = 100

interface Props {
  sessionId: string
  onBack: () => void
}

export function ReviewView({ sessionId, onBack }: Props) {
  const { data, isLoading } = useSessionTranscript(sessionId, true, INITIAL_TURN_LIMIT)
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)

  // Fold all transcript events into turns once the data arrives, then close the
  // replay: replayComplete stores earliestTurn/totalTurns (which drive the
  // scroll-up paging trigger) and clears the replaying flag.
  useEffect(() => {
    if (!data?.events) return
    for (const ev of data.events) {
      dispatch(ev)
    }
    dispatch({ type: 'replayComplete', earliestTurn: data.earliestTurn, totalTurns: data.totalTurns })
  }, [data])

  // Scroll-up paging — mirrors useSessionRuntime.onLoadOlder.
  const loadingOlderRef = useRef(false)
  const onLoadOlder = useCallback(() => {
    if (loadingOlderRef.current) return
    if (state.earliestTurn <= 0) return     // already at session start
    loadingOlderRef.current = true
    dispatch({ type: '_setLoadingOlder', value: true })
    fetchOlderTurns(sessionId, state.earliestTurn)
      .then((res) => {
        dispatch({ type: '_prependHistory', events: res.events, earliestTurn: res.earliestTurn })
      })
      .catch(() => {
        dispatch({ type: '_setLoadingOlder', value: false })
      })
      .finally(() => { loadingOlderRef.current = false })
  }, [sessionId, state.earliestTurn])

  return (
    <div className="flex flex-col h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      {/* paddingTop pushes content below the iOS status bar; env() is 0 on desktop. */}
      <header
        className={cn(
          'flex items-center gap-3 px-3 pb-3 border-b',
          'border-gray-200 dark:border-gray-700',
          'bg-white dark:bg-gray-800',
        )}
        style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.75rem)' }}
      >
        <button
          type="button"
          onClick={onBack}
          className={cn(
            'rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
          aria-label="Back to dashboard"
        >
          ← Back
        </button>
        <span className="text-sm text-gray-500 dark:text-gray-400 italic">
          Read-only (archived)
        </span>
      </header>

      {/* Transcript area */}
      {/* paddingBottom keeps last item above the home indicator; env() is 0 on desktop. */}
      <main className="flex-1 overflow-hidden flex flex-col" style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}>
        {isLoading ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">Loading transcript…</p>
          </div>
        ) : state.turns.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">No transcript available for this session.</p>
          </div>
        ) : (
          <TranscriptView
            turns={state.turns}
            toolDetailMode="auto"
            thinkingCollapsed={true}
            onAnswer={() => {/* read-only */}}
            earliestTurn={state.earliestTurn}
            loadingOlder={state.loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        )}
      </main>
    </div>
  )
}
