// web/src/features/sessions/list/SnoozedSection.tsx
//
// Collapsible section at the foot of the list for snoozed sessions — held out of
// the grouped list by `useListPipeline` (`isSnoozed`), soonest wake first.
// Collapsed by default: these are sessions the user asked not to see yet.
// Mirrors QueuedSection; the Wake control sits OUTSIDE the row's open button
// (a11y rule — no button nested inside a button).
import { useState } from 'react'
import { AlarmClock, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useWakeSession } from '@/api/hooks'
import { Button } from '@/ui/Button'
import { snoozeUntilLabel } from '@/session/snoozePresets'
import type { Session } from '@/api/types'

interface SnoozedSectionProps {
  /** Snoozed sessions, already sorted by the pipeline. */
  sessions: Session[]
  onOpen: (id: string) => void
}

export function SnoozedSection({ sessions, onOpen }: SnoozedSectionProps) {
  const [collapsed, setCollapsed] = useState(true)
  const { mutate: wake, isPending, variables } = useWakeSession()

  if (sessions.length === 0) return null

  return (
    <div>
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-controls="snoozed-section-panel"
        className={cn(
          'flex w-full min-h-11 [@media(hover:hover)]:min-h-0 items-center gap-1 px-1 py-1',
          'text-xs font-medium uppercase tracking-wide text-ink-muted',
          'hover:text-ink',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
        )}
      >
        {collapsed
          ? <ChevronRight size={14} aria-hidden="true" />
          : <ChevronDown size={14} aria-hidden="true" />}
        <AlarmClock size={14} aria-hidden="true" />
        <span>Snoozed ({sessions.length})</span>
      </button>

      {!collapsed && (
        <ul id="snoozed-section-panel" className="space-y-1 py-1">
          {sessions.map((session) => {
            const snooze = session.snooze
            const waking = isPending && variables === session.id
            return (
              <li
                key={session.id}
                className="flex items-center gap-2 rounded-lg border border-edge bg-surface px-2 py-2"
              >
                <button
                  type="button"
                  onClick={() => onOpen(session.id)}
                  className="flex-1 min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
                >
                  <div className="text-sm font-medium text-ink truncate">{session.name}</div>
                  <div className="text-xs text-ink-muted truncate">
                    {snoozeUntilLabel(snooze?.until)}
                    {snooze?.by === 'session' && <span> · self-snoozed</span>}
                  </div>
                  {snooze?.reason && (
                    <div className="text-xs text-ink-muted truncate" title={snooze.reason}>
                      {snooze.reason}
                    </div>
                  )}
                </button>
                <Button
                  variant="ghost"
                  size="sm"
                  loading={waking}
                  aria-label={`Wake ${session.name}`}
                  onClick={() => wake(session.id)}
                  className="h-auto shrink-0 min-h-11 [@media(hover:hover)]:min-h-0 px-2 py-1 text-xs text-accent"
                >
                  Wake
                </Button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
