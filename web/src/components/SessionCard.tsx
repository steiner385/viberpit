import { useRef, useEffect, useState } from 'react'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Play, Target } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { StatusPill, UsageBadge } from './UsageBadge'
import { PrPill } from './PrPill'
import { SubAgentRows } from './SubAgentRows'
import type { Session, Account } from '@/api/types'

// Shared sizing for the per-session icon buttons: 44px tap target on touch,
// a denser 32px on hover-capable (desktop) pointers.
const actionBtn = cn(
  'h-11 w-11 [@media(hover:hover)]:h-8 [@media(hover:hover)]:w-8',
  'flex items-center justify-center rounded-md',
  'focus-visible:outline-none focus-visible:ring-2',
)

interface SessionCardProps {
  session: Session
  account: Account | undefined
  selected?: boolean
  onOpen: (id: string) => void
  onRename: (id: string) => void
  /** Archive the session (stops the driver, moves it to the Archived tab). */
  onClose: (id: string) => void
  /** Permanently delete the session (irreversible; confirmed by the caller). */
  onDelete: (id: string) => void
  /** Quick-resume: text present → fire that suggestion in place; undefined → caller opens the session. */
  onQuickResume: (id: string, text?: string) => void
}

export function SessionCard({ session, account, selected = false, onOpen, onRename, onClose, onDelete, onQuickResume }: SessionCardProps) {
  const articleRef = useRef<HTMLElement>(null)
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const [expanded, setExpanded] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const suggestions = session.latestSuggestions ?? []
  const canFireInPlace = suggestions.length > 0 && !session.detached

  // Scroll selected row into view when selection changes (jsdom doesn't implement scrollIntoView)
  useEffect(() => {
    if (selected && articleRef.current?.scrollIntoView) {
      articleRef.current.scrollIntoView({ block: 'nearest' })
    }
  }, [selected])

  const repoLabel = formatRepoLabel(session.repo)

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen(session.id)
    }
  }

  return (
    <article
      ref={articleRef}
      className={cn(
        'group flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px]',
        'focus-within:ring-2 focus-within:ring-blue-500',
        selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
      )}
    >
     <div className="flex items-center gap-2">
      {hasSubAgents && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? 'Hide sub-agents' : 'Show sub-agents'}
          aria-expanded={expanded}
          className={cn(
            'flex-shrink-0 h-6 w-6 flex items-center justify-center rounded',
            'text-gray-400 hover:text-gray-600 dark:hover:text-gray-200',
            'hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
        >
          {expanded ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        </button>
      )}
      {/* Main tappable area — opens the session.
          id is used by DispatchBar's aria-activedescendant to signal the highlighted
          row to assistive technologies without moving DOM focus.
          data-selected is used for CSS + testing; aria-selected is omitted on a plain
          button because axe rejects it without role="option". The AT announcement
          comes from the textarea's aria-activedescendant pointing to this id. */}
      <button
        type="button"
        id={`row-${session.id}`}
        tabIndex={selected ? 0 : -1}
        data-selected={selected}
        className={cn(
          'flex-1 min-w-0 text-left min-h-[44px] flex flex-col justify-center gap-1',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        )}
        onClick={() => onOpen(session.id)}
        onKeyDown={handleKeyDown}
        aria-label={`Open session ${session.name}`}
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate"
            title={session.nameIsAuto ? 'Auto-named by Claude' : undefined}
          >
            {session.name}
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
            {account?.nickname || session.accountName}
          </span>
          {/* Sub-agent count, visible without expanding. Only shown while
              collapsed — once expanded, the chevron + SubAgentRows carry it. */}
          {hasSubAgents && !expanded && (
            <span className="text-xs tabular-nums px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
              {subAgents.length} {subAgents.length === 1 ? 'agent' : 'agents'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <span
            className={cn(
              'inline-flex items-center gap-1 text-xs max-w-full',
              repoLabel.isScratch
                ? 'text-gray-400 dark:text-gray-500 italic'
                : 'text-gray-600 dark:text-gray-300',
            )}
            title={repoLabel.text}
          >
            <FolderGit2 size={12} aria-hidden="true" className="flex-shrink-0" />
            <span className="truncate">{repoLabel.text}</span>
          </span>
          <StatusPill status={session.status} />
          {session.activeGoal && (
            <span
              className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 max-w-[12rem]"
              title={`Goal: ${session.activeGoal}`}
            >
              <Target size={12} aria-hidden="true" className="flex-shrink-0" />
              <span className="truncate">Goal</span>
            </span>
          )}
          {session.pr && <PrPill pr={session.pr} />}
          {/* Session usage only — weekly is an account-level figure shown in the
              Accounts tab and the in-session meta strip, so repeating it on every
              card just adds noise and forces the meta row to wrap. */}
          {account?.usage && (
            <UsageBadge
              pct={account.usage.session.pct}
              label="session"
              resetsAt={account.usage.session.resetsAt}
            />
          )}
          {/* Last activity — helps tell live sessions from stale ones at a glance.
              Refreshes naturally as React Query refetches the session list.
              Guarded against a missing/invalid timestamp so we never show "NaN". */}
          {isValidTimestamp(session.updatedAt) && (
            <span
              className="text-xs text-gray-400 dark:text-gray-500 tabular-nums"
              title={`Last active ${new Date(session.updatedAt).toLocaleString()}`}
            >
              active {relTime(session.updatedAt)}
            </span>
          )}
        </div>
      </button>

      {/* Per-session actions — explicit buttons (no overflow menu).
          On touch devices they stay visible at the 44px tap-target minimum. On
          hover-capable devices (desktop) they shrink to a denser 32px and stay
          hidden until the row is hovered, focused, or selected — so the meta row
          gets the full card width instead of fighting three permanent buttons. */}
      <div
        className={cn(
          'flex flex-shrink-0 items-center gap-0.5 transition-opacity',
          '[@media(hover:hover)]:opacity-0',
          '[@media(hover:hover)]:group-hover:opacity-100',
          '[@media(hover:hover)]:group-focus-within:opacity-100',
          selected && '[@media(hover:hover)]:opacity-100',
        )}
      >
        <button
          type="button"
          className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
            'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500')}
          aria-label={`Resume session ${session.name}`}
          title={canFireInPlace ? 'Resume — send the suggested next step' : 'Resume — open and continue'}
          onClick={() => onQuickResume(session.id, canFireInPlace ? suggestions[0] : undefined)}
        >
          <Play size={16} aria-hidden="true" />
        </button>
        {suggestions.length >= 2 && !session.detached && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label={`More suggestions for ${session.name}`}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
                'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500')}
              onClick={() => setMenuOpen((o) => !o)}
            >
              <ChevronDown size={16} aria-hidden="true" />
            </button>
            {menuOpen && (
              <div
                role="menu"
                className={cn(
                  'absolute top-full right-0 mt-1 min-w-[12rem] max-w-xs z-10',
                  'rounded-md border border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-800 shadow-lg py-1',
                )}
              >
                {suggestions.slice(1).map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onQuickResume(session.id, s) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <button
          type="button"
          className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500')}
          aria-label={`Rename session ${session.name}`}
          title="Rename"
          onClick={() => onRename(session.id)}
        >
          <Pencil size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500')}
          aria-label={`Archive session ${session.name}`}
          title="Archive"
          onClick={() => onClose(session.id)}
        >
          <Archive size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={cn(actionBtn, 'text-red-600 dark:text-red-400',
            'hover:bg-red-50 dark:hover:bg-red-900/20 focus-visible:ring-red-500')}
          aria-label={`Delete session ${session.name}`}
          title="Delete"
          onClick={() => onDelete(session.id)}
        >
          <Trash2 size={16} aria-hidden="true" />
        </button>
      </div>
     </div>
      {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
    </article>
  )
}
