// web/src/features/sessions/ScopeSegment.tsx
//
// Active | Archived — the sessions list's scope switch.
//
// It holds NO state. Scope is the route (`listScopeForPath`), so this is two
// links and a highlight: the Back button, a bookmark and a reload all land on
// the scope the URL names, and there is no second opinion to drift.
//
// Deliberately links rather than `role="tab"`: these navigate, and tab
// semantics would promise a tabpanel relationship that the router does not
// provide. `aria-current="page"` is the honest marker for "you are here".
//
// Tokens only — this file is under `src/features/`, where
// `ccswitch/no-raw-palette` is enforced.
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { ARCHIVED_PATH, SESSIONS_PATH, type ListScope } from '@/session/sessionNavState'

export interface ScopeSegmentProps {
  scope: ListScope
  /** Live sessions, pre-spawn rows included (the list-controls gate's count). */
  activeCount: number
  archivedCount: number
}

const item = cn(
  'flex-1 inline-flex min-h-target items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const here = 'bg-accent-wash text-accent'
const elsewhere = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

/** A zero count renders nothing rather than "0" — the same convention the nav
 *  badges use, and "Archived 0" reads as a counter that failed. */
function count(n: number) {
  return n > 0 ? <span className="tabular-nums text-xs">{n}</span> : null
}

export function ScopeSegment({ scope, activeCount, archivedCount }: ScopeSegmentProps) {
  return (
    <nav
      aria-label="Session scope"
      className="flex gap-1 rounded-md border border-edge bg-surface p-1"
    >
      <Link
        to={SESSIONS_PATH}
        aria-current={scope === 'live' ? 'page' : undefined}
        className={cn(item, scope === 'live' ? here : elsewhere)}
      >
        Active
        {count(activeCount)}
      </Link>
      <Link
        to={ARCHIVED_PATH}
        aria-current={scope === 'archived' ? 'page' : undefined}
        className={cn(item, scope === 'archived' ? here : elsewhere)}
      >
        Archived
        {count(archivedCount)}
      </Link>
    </nav>
  )
}
