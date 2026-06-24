import { useRef, useEffect, useState } from 'react'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Play, Target } from 'lucide-react'
import { matchListShortcut } from '@/lib/shortcuts'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { statusTier } from '@/session/statusTier'
import { StatusPill, UsageBadge } from './UsageBadge'
import { PrList } from './PrList'
import { SubAgentRows } from './SubAgentRows'
import type { Session, Account } from '@/api/types'

// Shared sizing for the per-session icon buttons: 44px tap target on touch,
// a denser 32px on hover-capable (desktop) pointers.
const actionBtn = cn(
  'h-11 w-11 [@media(hover:hover)]:h-8 [@media(hover:hover)]:w-8',
  'flex items-center justify-center rounded-md',
  'focus-visible:outline-none focus-visible:ring-2',
  // Press feedback: a quick scale-down + background darken so a tap/click
  // is unmistakable (hover alone gives no change once the cursor is already
  // over the button, and touch has no hover at all). active: fires on both.
  'transition-[background-color,transform] duration-100',
  'active:scale-90 active:duration-75',
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
  /** When true, the highlighted row takes real DOM focus (list-focus mode). */
  listMode?: boolean
  /** Move the highlight to the previous/next row (list mode). */
  onNavigate?: (dir: 'up' | 'down') => void
  /** Leave list mode and return focus to the prompt. */
  onExit?: () => void
}

export function SessionCard({ session, account, selected = false, listMode = false, onOpen, onRename, onClose, onDelete, onQuickResume, onNavigate, onExit }: SessionCardProps) {
  const articleRef = useRef<HTMLElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const [expanded, setExpanded] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const suggestions = session.latestSuggestions ?? []
  // One-tap resume sends the cached top suggestion if there is one, else "continue".
  // The backend lazily resumes a detached session before delivering, so this works
  // on idle/"Ready" sessions too. Hidden while the session is actively working.
  const resumeText = suggestions[0] ?? 'continue'
  const isBusy = statusTier(session.status) === 'busy'

  // Scroll selected row into view when selection changes (jsdom doesn't implement scrollIntoView)
  useEffect(() => {
    if (selected && articleRef.current?.scrollIntoView) {
      articleRef.current.scrollIntoView({ block: 'nearest' })
    }
  }, [selected])

  // In list-focus mode, give the highlighted row REAL keyboard focus so single-key
  // shortcuts land here (and never type into the prompt).
  useEffect(() => {
    if (listMode && selected) buttonRef.current?.focus()
  }, [listMode, selected])

  const repoLabel = formatRepoLabel(session.repo)

  function handleKeyDown(e: React.KeyboardEvent) {
    // Single-key shortcuts are only live in list-focus mode. Outside it the row can
    // still hold native focus (tabIndex 0 when selected), so guard against stray
    // destructive keypresses — keep only the Enter/Space "open" affordance.
    if (!listMode) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        onOpen(session.id)
      }
      return
    }
    const action = matchListShortcut(e)
    if (!action) return
    e.preventDefault()
    switch (action.type) {
      case 'open': onOpen(session.id); break
      case 'continue': onQuickResume(session.id, resumeText); break
      case 'rename': onRename(session.id); break
      case 'archive': onClose(session.id); break
      case 'permanent-delete': onDelete(session.id); break
      case 'navigate': onNavigate?.(action.dir); break
      case 'exit': onExit?.(); break
    }
  }

  if (session.pending) {
    return (
      <article
        data-testid="session-card-pending"
        aria-busy="true"
        className={cn(
          'group flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
          'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px]',
          'opacity-70 cursor-default select-none',
        )}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="inline-block h-3 w-3 flex-shrink-0 animate-spin rounded-full border-2 border-gray-400 border-t-transparent"
            aria-hidden="true"
          />
          <span className="flex-1 min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100">
            {session.name}
          </span>
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Starting…</p>
      </article>
    )
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
        ref={buttonRef}
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
        {/* The card lives in a drag-resizable sidebar, so the layout must read
            identically at every width: three fixed rows, each truncating rather
            than wrapping. Flex-wrap was content-dependent — the account chip and
            usage bar landed in a different place on every card. Truncation +
            `min-w-0` collapses uniformly at any sidebar/viewport width with no
            breakpoints, so every card shares one skeleton. */}

        {/* Row 1 — identity: title truncates, account chip pinned right. */}
        <div data-testid="card-identity" className="flex items-center gap-2 min-w-0">
          <span
            className="flex-1 min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            title={session.nameIsAuto ? 'Auto-named by Claude' : undefined}
          >
            {session.name}
          </span>
          {/* Sub-agent count, visible without expanding. Only shown while
              collapsed — once expanded, the chevron + SubAgentRows carry it. */}
          {hasSubAgents && !expanded && (
            <span className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
              {subAgents.length} {subAgents.length === 1 ? 'agent' : 'agents'}
            </span>
          )}
          {/* The chip yields before the title does: capped at 45% of the row and
              truncating, so a long account email can't starve the session name on
              a narrow (mobile) width. `title` keeps the full value reachable. */}
          <span
            className="flex-shrink-0 max-w-[45%] truncate text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded"
            title={account?.nickname || session.accountName}
          >
            {account?.nickname || session.accountName}
          </span>
        </div>
        {/* Row 2 — context: repo gives way (truncates) so the status pill and
            goal/PR markers stay visible. Repo anchors the left and absorbs slack
            (flex-1 min-w-0 → truncates under pressure); the state chips form a
            single non-shrinking group anchored to the right, so the status pill
            always lands at the same edge regardless of repo length. */}
        <div data-testid="card-context" className="flex items-center gap-2 min-w-0">
          <span
            className={cn(
              'flex-1 min-w-0 inline-flex items-center gap-1 text-xs',
              repoLabel.isScratch
                ? 'text-gray-400 dark:text-gray-500 italic'
                : 'text-gray-600 dark:text-gray-300',
            )}
            title={repoLabel.text}
          >
            <FolderGit2 size={12} aria-hidden="true" className="flex-shrink-0" />
            <span className="truncate">{repoLabel.text}</span>
          </span>
          <div className="flex flex-shrink-0 items-center gap-2">
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
            <PrList prs={session.prs} />
          </div>
        </div>

        {/* Row 3 — telemetry: session usage left, last-active pinned right via
            `ml-auto` so the timestamp sits in the same spot whether or not a
            usage bar is present. Weekly usage is omitted on purpose — it's an
            account-level figure shown in the Accounts tab and in-session meta
            strip, so repeating it per card is just noise. The row renders only
            when there's something to show, but at least one is near-always
            present (every live session has an updatedAt). */}
        {(account?.usage || (!isBusy && isValidTimestamp(session.updatedAt))) && (
          <div data-testid="card-telemetry" className="flex items-center gap-2 min-w-0">
            {account?.usage && (
              <UsageBadge
                pct={account.usage.session.pct}
                label="session"
                resetsAt={account.usage.session.resetsAt}
              />
            )}
            {/* Last activity — helps tell live sessions from stale ones at a
                glance. Refreshes as React Query refetches the session list.
                Guarded against a missing/invalid timestamp so we never show
                "NaN" or the 1970 epoch. Suppressed while busy: `last_turn_at`
                (its source) freezes for the whole in-progress turn, so it would
                read "active 10m ago" on a session that is actively streaming —
                stale-looking next to its live "Working…" spinner, which already
                conveys liveness. */}
            {!isBusy && isValidTimestamp(session.updatedAt) && (
              <span
                className="ml-auto flex-shrink-0 text-xs text-gray-400 dark:text-gray-500 tabular-nums"
                title={`Last active ${new Date(session.updatedAt).toLocaleString()}`}
              >
                active {relTime(session.updatedAt)}
              </span>
            )}
          </div>
        )}
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
        {!isBusy && (
          <button
            type="button"
            className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
              'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500',
              'active:bg-emerald-100 dark:active:bg-emerald-900/40')}
            aria-label={`Resume session ${session.name}`}
            title={suggestions.length > 0 ? 'Resume — send the suggested next step' : 'Resume — send “continue”'}
            onClick={() => onQuickResume(session.id, resumeText)}
          >
            <Play size={16} aria-hidden="true" />
          </button>
        )}
        {!isBusy && suggestions.length >= 2 && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label={`More suggestions for ${session.name}`}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              className={cn(actionBtn, 'text-emerald-600 dark:text-emerald-400',
                'hover:bg-emerald-50 dark:hover:bg-emerald-900/20 focus-visible:ring-emerald-500',
              'active:bg-emerald-100 dark:active:bg-emerald-900/40')}
              onClick={() => setMenuOpen((o) => !o)}
            >
              <ChevronDown size={16} aria-hidden="true" />
            </button>
            {menuOpen && (
              <div
                role="menu"
                className={cn(
                  // z-20 keeps the open menu above the sticky group headers
                  // (GroupHeader: `sticky top-0 z-10`); at an equal z-index a later
                  // group's header would paint over a menu opened from a card above it.
                  'absolute top-full right-0 mt-1 min-w-[12rem] max-w-xs z-20',
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
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500',
            'active:bg-gray-200 dark:active:bg-gray-600')}
          aria-label={`Rename session ${session.name}`}
          title="Rename"
          onClick={() => onRename(session.id)}
        >
          <Pencil size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500',
            'active:bg-gray-200 dark:active:bg-gray-600')}
          aria-label={`Archive session ${session.name}`}
          title="Archive"
          onClick={() => onClose(session.id)}
        >
          <Archive size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={cn(actionBtn, 'text-red-600 dark:text-red-400',
            'hover:bg-red-50 dark:hover:bg-red-900/20 focus-visible:ring-red-500',
            'active:bg-red-100 dark:active:bg-red-900/40')}
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
