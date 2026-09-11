import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { Session } from '@/api/types'

const DOT: Record<string, string> = {
  in_progress: 'bg-blue-500 animate-pulse', busy: 'bg-blue-500 animate-pulse',
  waiting_question: 'bg-amber-500', waiting_input: 'bg-amber-500', ready_review: 'bg-amber-500',
  needs_attention: 'bg-red-500',
  complete: 'bg-emerald-500', done: 'bg-emerald-500',
}

/**
 * Rows for a parent card's child sessions (child-session spec §4.6). Each child is a
 * REAL session, so a row navigates to it — unlike SubAgentRows, whose rows open the
 * read-only-ish sub-agent drill-down inside the parent.
 */
export function ChildSessionRows({ children }: { children: Session[] }) {
  const navigate = useNavigate()
  const scope = listScopeForPath(useLocation().pathname)
  return (
    <ul className="flex flex-col py-1" aria-label="Child sessions">
      {children.map((c) => (
        <li key={c.id}>
          <button
            type="button"
            onClick={() => navigate(sessionPath(c.id, scope))}
            aria-label={`Open child session ${c.name}`}
            className={cn(
              'flex w-full items-center gap-2 py-1.5 pl-3 pr-2 text-left text-xs min-h-[36px] rounded',
              'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            )}
          >
            <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', DOT[c.status] ?? 'bg-gray-400')} />
            <span className="font-medium text-gray-800 dark:text-gray-200 truncate">{c.name}</span>
            <span className="ml-auto text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">
              {c.status}
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}
