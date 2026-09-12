import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import { statusPresentation, TIER_ACCENT } from '@/session/statusTier'
import type { Session } from '@/api/types'

/**
 * Rows for a parent card's child sessions (child-session spec §4.6). Each child is a
 * REAL session, so a row navigates to it — unlike SubAgentRows, whose rows open the
 * sub-agent drill-down inside the parent. Status colour and label come from
 * `statusPresentation` / `TIER_ACCENT`, the one status → presentation table.
 */
export function ChildSessionRows({ sessions }: { sessions: Session[] }) {
  const navigate = useNavigate()
  const scope = listScopeForPath(useLocation().pathname)
  return (
    <ul className="flex flex-col py-1" aria-label="Child sessions">
      {sessions.map((c) => {
        const p = statusPresentation(c.status, { interruptedByRestart: c.interruptedByRestart })
        const accent = TIER_ACCENT[p.tier]
        return (
          <li key={c.id}>
            <button
              type="button"
              onClick={() => navigate(sessionPath(c.id, scope))}
              aria-label={`Open child session ${c.name}`}
              className={cn(
                'flex w-full items-center gap-2 py-1.5 pl-3 pr-2 text-left text-xs min-h-9 rounded',
                'text-ink-muted hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
            >
              <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', accent.dot, accent.pulse && 'animate-pulse')} />
              <span className="font-medium text-ink truncate">{c.name}</span>
              <span className={cn('ml-auto text-2xs uppercase tracking-wide', p.textClass)}>{p.label}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
