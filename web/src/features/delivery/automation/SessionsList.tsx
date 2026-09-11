/**
 * The Sessions tab: sessions this automation spawned. Moved from
 * `AutomationsTab.tsx`'s `SpawnedSessionsSection`, but rows now navigate via a
 * real `Link` (`sessionNavState.ts`) instead of an `onOpenSession`/
 * `onReviewSession` callback pair — the detail pane has a URL of its own to
 * link out from, where the old monitor list did not.
 */
import { Link } from 'react-router-dom'
import type { Automation } from '@/api/automations'
import { useAutomationSessions } from '@/api/hooks'
import { INACTIVE_SESSION_STATUSES } from '@/api/types'
import { archivedSessionPath, sessionPath } from '@/session/sessionNavState'

export function SessionsList({ automation: a }: { automation: Automation }) {
  const sessions = useAutomationSessions(a.id)
  const list = sessions.data ?? []
  const activeCount = list.filter((s) => !INACTIVE_SESSION_STATUSES.has(s.status)).length

  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">
        Spawned Sessions{list.length > 0 && ` (${activeCount} active)`}
      </h4>
      {list.length === 0
        ? <p className="text-sm text-ink-muted">No sessions spawned.</p>
        : (
          <div className="max-h-40 overflow-y-auto rounded-md border border-edge p-2">
            <ul className="space-y-1 text-sm">
              {list.map((s) => (
                <li key={s.id} className="flex items-center gap-2">
                  <Link
                    to={INACTIVE_SESSION_STATUSES.has(s.status)
                      ? archivedSessionPath(s.id)
                      : sessionPath(s.id, a.workClass === 'maintenance' ? 'background' : 'live')}
                    className="text-accent hover:underline text-left"
                  >
                    {s.name}
                  </Link>
                  <span className="text-ink-muted">· {s.status}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
    </section>
  )
}
