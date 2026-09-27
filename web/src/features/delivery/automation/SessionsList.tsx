/**
 * The Sessions tab: sessions this automation spawned. Rows navigate via a real
 * `Link` (`sessionNavState.ts`) — the detail pane has a URL of its own to link
 * out from. The tab carries the count (its badge, set by `AutomationDetail`
 * from this same query); the panel adds only what the badge cannot say, how
 * many are still active. No inner scroll box: the page scrolls.
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

  if (list.length === 0) return <p className="text-sm text-ink-muted">No sessions spawned.</p>
  return (
    <div className="space-y-2">
      <p className="text-xs text-ink-muted">{activeCount} active</p>
      <ul className="space-y-1 text-sm" aria-label="Spawned sessions">
        {list.map((s) => (
          <li key={s.id} className="flex flex-wrap items-baseline gap-x-2">
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
  )
}
