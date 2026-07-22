// web/src/components/QueuedSection.tsx
//
// Collapsible sidebar section listing sessions waiting in the max-active-sessions
// queue, sorted by position. Move/cancel controls are in a dedicated actions group
// (NEVER nested inside the row's open button — a11y rule).
import { useState } from 'react'
import { ArrowUp, ArrowDown, ChevronsUp, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useQueueMove, useQueueCancel } from '@/api/hooks'
import type { Session } from '@/api/types'

interface QueuedSectionProps {
  sessions: Session[]
  onOpen: (id: string) => void
}

export function QueuedSection({ sessions, onOpen }: QueuedSectionProps) {
  const [collapsed, setCollapsed] = useState(false)
  const { mutate: queueMove } = useQueueMove()
  const { mutate: queueCancel } = useQueueCancel()

  // Pre-sorted by the host (brief says caller sorts), but sort defensively here too.
  const sorted = [...sessions].sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0))

  if (sorted.length === 0) return null

  return (
    <div>
      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-controls="queued-section-panel"
        className={cn(
          'sticky top-0 z-10 flex w-full items-center gap-2 px-1 py-1',
          'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
          'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
          'hover:text-gray-700 dark:hover:text-gray-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        )}
      >
        <span>Queued ({sorted.length})</span>
      </button>

      {!collapsed && (
        <ul id="queued-section-panel" className="space-y-1 py-1">
          {sorted.map((session, idx) => {
            const isFirst = idx === 0
            const isLast = idx === sorted.length - 1
            return (
              <li
                key={session.id}
                className={cn(
                  'relative flex items-center gap-2 rounded-lg border px-2 py-2',
                  'border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-900',
                )}
              >
                {/* Position badge */}
                <span className="shrink-0 text-xs tabular-nums text-gray-400 dark:text-gray-500 w-5 text-center">
                  {session.queuePosition ?? idx + 1}
                </span>

                {/* Open button — wraps ONLY name/label block */}
                <button
                  type="button"
                  onClick={() => onOpen(session.id)}
                  className="flex-1 min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded"
                >
                  <div className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                    {session.name}
                  </div>
                  {session.repo && (
                    <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
                      {session.repo}
                    </div>
                  )}
                </button>

                {/* Actions group — OUTSIDE the open button */}
                <div role="group" className="flex shrink-0 items-center gap-0.5">
                  <button
                    type="button"
                    aria-label="Move to top"
                    disabled={isFirst}
                    onClick={() => queueMove({ id: session.id, op: 'top' })}
                    className={cn(
                      'rounded p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                      'disabled:opacity-30 disabled:cursor-not-allowed',
                    )}
                  >
                    <ChevronsUp size={14} aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label="Move up"
                    disabled={isFirst}
                    onClick={() => queueMove({ id: session.id, op: 'up' })}
                    className={cn(
                      'rounded p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                      'disabled:opacity-30 disabled:cursor-not-allowed',
                    )}
                  >
                    <ArrowUp size={14} aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label="Move down"
                    disabled={isLast}
                    onClick={() => queueMove({ id: session.id, op: 'down' })}
                    className={cn(
                      'rounded p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                      'disabled:opacity-30 disabled:cursor-not-allowed',
                    )}
                  >
                    <ArrowDown size={14} aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label="Remove from queue"
                    onClick={() => queueCancel({ id: session.id })}
                    className={cn(
                      'rounded p-1 text-gray-400 hover:text-red-600 dark:hover:text-red-400',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                    )}
                  >
                    <X size={14} aria-hidden />
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
