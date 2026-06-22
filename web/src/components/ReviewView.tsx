/**
 * ReviewView — read-only transcript viewer for archived sessions.
 *
 * Fetches the session transcript via useSessionTranscript, folds all
 * events through transcriptReducer to produce turns, and renders them
 * with TranscriptView in read-only mode (no composer, no WS connection).
 */

import { useReducer, useEffect } from 'react'
import { cn } from '@/lib/utils'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import { useSessionTranscript } from '@/api/hooks'
import { TranscriptView } from './session/TranscriptView'

interface Props {
  sessionId: string
  onBack: () => void
}

export function ReviewView({ sessionId, onBack }: Props) {
  const { data, isLoading } = useSessionTranscript(sessionId, true)
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)

  // Fold all transcript events into turns once the data arrives.
  useEffect(() => {
    if (!data?.events) return
    for (const ev of data.events) {
      dispatch(ev)
    }
  }, [data])

  return (
    <div className="flex flex-col h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <header className={cn(
        'flex items-center gap-3 p-3 border-b',
        'border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800',
      )}>
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
      <main className="flex-1 overflow-hidden flex flex-col">
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
            permissionMode="interactive"
            toolDetailMode="auto"
            thinkingCollapsed={true}
            onDecision={() => {/* read-only */}}
            onAnswer={() => {/* read-only */}}
          />
        )}
      </main>
    </div>
  )
}
