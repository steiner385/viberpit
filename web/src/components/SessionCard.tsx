import { useRef, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Sparkles, Loader2, Target, Wrench, Bot } from 'lucide-react'
import { toast } from 'sonner'
import { matchListShortcut } from '@/lib/shortcuts'
import { QUICK_REPLIES } from '@/lib/quickReplies'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { statusTier } from '@/session/statusTier'
import { anyChecksFailing, isActionablePrState } from '@/session/prStatus'
import { StatusPill, UsageBadge } from './UsageBadge'
import { PrList } from './PrList'
import { SubAgentRows } from './SubAgentRows'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useSwipeAction } from '@/lib/useSwipeAction'
import { useNow } from '@/hooks/useNow'
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

// Human-readable labels for automation ids in the provenance badge — one map for
// spawned-by and assisted-by (the pr-doctor special case previously lived inline).
const AUTOMATION_LABELS: Record<string, string> = {
  'pr-doctor': 'PR Doctor',
  'backlog-runner': 'Backlog Runner',
  autopilot: 'Autopilot',
}

function automationLabel(id: string): string {
  return AUTOMATION_LABELS[id] ?? id
}

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
  /** Sparkle (✨): generate a fresh next-prompt via the LLM. Resolves to the suggestion
   *  strings; the card sends the top one. Hard-coded replies stay in the dropdown only. */
  onGenerateSuggestion?: (id: string) => Promise<string[]>
  /** When true, the highlighted row takes real DOM focus (list-focus mode). */
  listMode?: boolean
  /** Move the highlight to the previous/next row (list mode). */
  onNavigate?: (dir: 'up' | 'down') => void
  /** Leave list mode and return focus to the prompt. */
  onExit?: () => void
  /** PR autofix mode from app settings: 'propose' | 'auto' | 'off' */
  prAutofixMode?: string
  /** Called when the user taps "Fix PR #N" — parent owns the mutation. */
  onFixPr?: (id: string, number: number) => void
  /** Called when the user toggles this session's autopilot opt-out — parent owns the mutation. */
  onToggleAutopilot?: (id: string, paused: boolean) => void
}

export function SessionCard({ session, account, selected = false, listMode = false, onOpen, onRename, onClose, onDelete, onQuickResume, onGenerateSuggestion, onNavigate, onExit, prAutofixMode, onFixPr, onToggleAutopilot }: SessionCardProps) {
  const articleRef = useRef<HTMLElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const [expanded, setExpanded] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [generating, setGenerating] = useState(false)

  const suggestions = session.latestSuggestions ?? []
  const isBusy = statusTier(session.status) === 'busy'

  // "Guided by autopilot": the robot has acted on this session (assistedBy) AND it
  // isn't pinned to manual. The pause veto matters — a session can carry the
  // autopilot provenance yet be paused now, so a green "being driven" signal would
  // contradict the grey "Manual" badge. autopilotLive narrows that to "acting right
  // now" (busy turn), which is what earns the pulse.
  const guidedByAutopilot = session.assistedBy === 'autopilot' && !session.autopilotPaused
  const autopilotLive = guidedByAutopilot && isBusy

  // Sparkle (✨) tap ALWAYS asks the LLM for a fresh next-prompt, then sends the
  // top result — never a cached value or a hard-coded fallback (those live only in
  // the dropdown). The backend lazily resumes a detached session before delivering.
  // When the model returns nothing we surface a hint instead of silently sending
  // "continue"; the dropdown's canned replies are the manual escape hatch.
  async function handleSparkle() {
    if (!onGenerateSuggestion || generating) return
    setGenerating(true)
    try {
      const items = await onGenerateSuggestion(session.id)
      if (items.length > 0) {
        onQuickResume(session.id, items[0])
      } else {
        toast.info('No suggestion right now — pick a quick reply from the menu.')
      }
    } catch {
      toast.error('Could not generate a suggestion.')
    } finally {
      setGenerating(false)
    }
  }

  const failingPr = (session.prs ?? [])
    .filter((p) => anyChecksFailing([p]) && isActionablePrState(p.state))
    .sort((a, b) => a.number - b.number)[0]
  const showFix = prAutofixMode === 'propose' && !!failingPr

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
  const now = useNow()

  // Touch-only swipe actions. lg = the app's desktop breakpoint (App.tsx); jsdom
  // reports false → mobile, so tests exercise this branch by default.
  const isWide = useMediaQuery('(min-width: 1024px)')
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const swipeEnabled = !isWide && !session.pending
  const swipe = useSwipeAction({
    enabled: swipeEnabled,
    // Swipe left → Delete (destructive, confirmed by onDelete's dialog).
    // Swipe right → Archive (reversible).
    onCommit: (side) => (side === 'left' ? onDelete(session.id) : onClose(session.id)),
  })
  // Resting offset: follow the finger while dragging, else rest open at the panel
  // width (negative = left/delete revealed), else closed.
  const PANEL_PX = 88
  const restX = swipe.dragging
    ? swipe.dx
    : swipe.openSide === 'left' ? -PANEL_PX : swipe.openSide === 'right' ? PANEL_PX : 0

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
      case 'continue': void handleSparkle(); break
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
        // `@container` makes the card a container-query context so the layout
        // below responds to the card's OWN width (i.e. the drag-resizable
        // sidebar), not the viewport. A narrow sidebar on a wide (≥1024px)
        // window used to keep the full desktop layout — all badges + the 6-icon
        // grid — crammed into a tiny box, so the badge row overflowed under the
        // action icons. Container queries (`@lg:*` below) collapse to the
        // compact layout by real available width instead.
        'group @container flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px]',
        'focus-within:ring-2 focus-within:ring-blue-500',
        // Autopilot-driven cards get an emerald left edge so they pop out of the
        // list at a glance — costs no horizontal space, so it reads identically on
        // mobile and desktop. The side-specific border-l color wins over the base
        // border-gray; the `selected` ring sits outside the border and is unaffected.
        guidedByAutopilot && 'border-l-4 border-l-emerald-500 dark:border-l-emerald-400',
        selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
        !isWide && 'relative overflow-hidden', // clip the edge panels; mobile only so the desktop quick-reply menu is never clipped
      )}
    >
      {/* Edge panels mount only while a swipe is in flight or held open. The
          foreground sits inside the article's px-3/py-2 padding, so panels
          pinned to the border box (inset-0) would otherwise peek out around it
          at rest — a permanent red/grey band on every card. */}
      {!isWide && (swipe.dragging || swipe.openSide !== null) && (
        <>
          {/* Archive panel — left edge, revealed by swiping right (openSide==='right'). */}
          <button
            type="button"
            data-testid="swipe-panel-archive"
            aria-label={`Archive ${session.name}`}
            // Hidden from assistive tech (not just tab order) until revealed, so a
            // screen reader doesn't announce a phantom action behind every card.
            aria-hidden={swipe.openSide !== 'right'}
            tabIndex={swipe.openSide === 'right' ? 0 : -1}
            onClick={() => { swipe.reset(); onClose(session.id) }}
            className={cn(
              'absolute inset-y-0 left-0 flex w-[88px] items-center justify-center',
              'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
              swipe.openSide === 'right' ? 'pointer-events-auto' : 'pointer-events-none',
            )}
          >
            <Archive size={20} aria-hidden="true" />
          </button>
          {/* Delete panel — right edge, revealed by swiping left (openSide==='left'). */}
          <button
            type="button"
            data-testid="swipe-panel-delete"
            aria-label={`Delete ${session.name}`}
            aria-hidden={swipe.openSide !== 'left'}
            tabIndex={swipe.openSide === 'left' ? 0 : -1}
            onClick={() => { swipe.reset(); onDelete(session.id) }}
            className={cn(
              'absolute inset-y-0 right-0 flex w-[88px] items-center justify-center',
              'bg-red-600 text-white',
              swipe.openSide === 'left' ? 'pointer-events-auto' : 'pointer-events-none',
            )}
          >
            <Trash2 size={20} aria-hidden="true" />
          </button>
        </>
      )}

      {/* Foreground — the existing card body. On mobile it translates over the
          panels; on desktop it is a plain wrapper with no gesture handlers. */}
      <div
        {...(!isWide ? { 'data-testid': 'card-swipe-fg', onPointerDown: swipe.onPointerDown } : {})}
        style={!isWide ? { transform: `translateX(${restX}px)` } : undefined}
        className={cn(
          !isWide && 'relative bg-white dark:bg-gray-800 touch-pan-y',
          !isWide && !swipe.dragging && !reduceMotion && 'transition-transform duration-200',
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
        onClick={() => {
          if (swipe.openSide) { swipe.reset(); return }   // tap closes a revealed card
          if (swipe.consumeDrag()) return                  // swallow the click that ends a drag
          onOpen(session.id)
        }}
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
          {/* Autopilot marker — the mobile-visible eye-catcher (the provenance badge
              below is desktop-only). Non-interactive, so it's safe inside the
              open-session button. Pulses only while the robot is actively working
              (autopilotLive), and degrades to a static glyph under reduced-motion. */}
          {guidedByAutopilot && (
            <span
              data-testid="card-autopilot-active"
              className={cn(
                'flex-shrink-0 inline-flex items-center text-emerald-600 dark:text-emerald-400',
                autopilotLive && !reduceMotion && 'animate-pulse',
              )}
              title={autopilotLive ? 'Autopilot active — working now' : 'Guided by Autopilot'}
            >
              <Bot size={14} aria-hidden="true" />
              <span className="sr-only">
                {autopilotLive ? 'Autopilot active, working now' : 'Guided by Autopilot'}
              </span>
            </span>
          )}
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
        {/* Summary — one muted truncated line of "what's happening now", below the
            name. Hidden when absent so dormant cards keep their compact height. */}
        {session.summary && (
          <div
            data-testid="card-summary"
            className="truncate text-xs text-gray-500 dark:text-gray-400"
            title={session.summary}
          >
            {session.summary}
          </div>
        )}
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
          {/* Status pill is the one context signal kept at every width; the Goal
              badge and the PR badge are secondary, so they are gated to a wide
              card (`hidden @lg:*`, container-query on the card ≥ 32rem) — in a
              narrow sidebar they bunched against the status pill and forced the
              repo label to a few characters. They remain reachable in the
              session detail view. */}
          <div className="flex flex-shrink-0 items-center gap-2">
            <StatusPill status={session.status} interruptedByRestart={session.interruptedByRestart} />
            {session.activeGoal && (
              <span
                data-testid="card-goal"
                className="hidden @lg:inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 max-w-[12rem]"
                title={`Goal: ${session.activeGoal}`}
              >
                <Target size={12} aria-hidden="true" className="flex-shrink-0" />
                <span className="truncate">Goal</span>
              </span>
            )}
            {!session.activeGoal && session.goalSource === 'inferred' && session.effectiveGoal && (
              /* Subdued variant: the goal is LLM-inferred, not user-set — outline
                 styling distinguishes it from the solid explicit-/goal pill. */
              <span
                data-testid="card-goal-inferred"
                className="hidden @lg:inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border border-indigo-200 dark:border-indigo-800 text-indigo-500 dark:text-indigo-400 max-w-[12rem]"
                title={`Goal (inferred): ${session.effectiveGoal}`}
              >
                <Target size={12} aria-hidden="true" className="flex-shrink-0" />
                <span className="truncate">Goal</span>
              </span>
            )}
            {session.autopilotPaused && (
              <span
                data-testid="card-autopilot-badge"
                className="hidden @lg:inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400"
                title="Autopilot paused — this session is on manual"
              >
                <Bot size={12} aria-hidden="true" className="flex-shrink-0" />
                <span className="truncate">Manual</span>
              </span>
            )}
            {session.autoArchiveOptOut && (
              /* Passive indicator (not interactive — a11y): this session was manually
                 unarchived, so autopilot will never auto-archive it again. */
              <span
                data-testid="card-no-autoarchive-badge"
                className="hidden @lg:inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300"
                title="Auto-archive off — you unarchived this session, so autopilot won't archive it again"
              >
                <Archive size={12} aria-hidden="true" className="flex-shrink-0" />
                <span className="truncate">Auto-archive off</span>
              </span>
            )}
            {(session.origin || session.assistedBy) && (
              /* Provenance: one violet badge for any automation involvement.
                 Spawned-by (origin) outranks assisted-by when both apply. */
              <Link
                to="/automation"
                data-testid="card-provenance"
                onClick={(e) => e.stopPropagation()}
                className={cn(
                  'hidden @lg:inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 max-w-[12rem]',
                  // Emerald when autopilot is actively guiding (matches the edge +
                  // marker); generic violet for created-by/other automation, and for
                  // an autopilot session that's paused (guidedByAutopilot is false).
                  guidedByAutopilot && !session.origin
                    ? 'bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-200 dark:hover:bg-emerald-900/60'
                    : 'bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 hover:bg-violet-200 dark:hover:bg-violet-900/60',
                )}
                title={session.origin
                  ? `Created by automation: ${session.origin}`
                  : `Assisted by automation: ${session.assistedBy}`}
              >
                <Bot size={12} aria-hidden="true" className="flex-shrink-0" />
                <span className="truncate">
                  {session.origin
                    ? `Created by ${automationLabel(session.origin)}`
                    : `Assisted by ${automationLabel(session.assistedBy!)}`}
                </span>
              </Link>
            )}
            {session.prs?.length > 0 && (
              <span data-testid="card-prs" className="hidden @lg:inline-flex items-center">
                <PrList prs={session.prs} />
              </span>
            )}
            {showFix && failingPr && (
              <button
                type="button"
                data-testid="card-fix-pr"
                onClick={(e) => { e.stopPropagation(); onFixPr?.(session.id, failingPr.number) }}
                aria-label={`Fix PR #${failingPr.number}`}
                className="hidden @lg:inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              >
                <Wrench size={11} aria-hidden />
                <span>Fix PR #{failingPr.number}</span>
              </button>
            )}
          </div>
        </div>

        {/* Row 3 — telemetry: session usage left, last-active pinned right via
            `ml-auto` so the timestamp sits in the same spot whether or not a
            usage bar is present. Weekly usage is omitted on purpose — it's an
            account-level figure shown in the Accounts tab and in-session meta
            strip, so repeating it per card is just noise. The row renders only
            when there's something to show, but at least one is near-always
            present (every live session has an updatedAt). */}
        {/* The whole telemetry row (usage % + "active … ago") shows only on a
            wide card: `hidden @lg:flex`. It's the least glanceable line in a
            narrow sidebar and the biggest source of vertical density; the usage
            figure also lives in the Accounts tab and the in-session meta strip. */}
        {(account?.usage || (!isBusy && isValidTimestamp(session.updatedAt))) && (
          <div data-testid="card-telemetry" className="hidden @lg:flex items-center gap-2 min-w-0">
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
                active {relTime(session.updatedAt, now || undefined)}
              </span>
            )}
          </div>
        )}
      </button>

      {/* Per-session actions — explicit buttons (no overflow menu).
          On touch devices they stay visible at the 44px tap-target minimum. On
          hover-capable devices (desktop) they shrink to a denser 32px and stay
          hidden until the row is hovered, focused, or selected — so the meta row
          gets the full card width instead of fighting three permanent buttons.
          In a narrow card only Resume stays inline; the rest are `hidden @lg:*`
          (see the secondary-actions group below) and live in the detail view.
          On a wide card (container ≥ 32rem) the buttons lay out as a 3-column
          grid so the (up to six) controls fold into two rows — halving the
          horizontal footprint they steal from the meta row. The secondary group
          uses `@lg:contents` so its five children flow into this grid directly
          rather than as one wide flex child. */}
      <div
        className={cn(
          'flex flex-shrink-0 items-center gap-0.5 transition-opacity',
          '@lg:grid @lg:grid-cols-3 @lg:justify-items-center',
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
              'active:bg-emerald-100 dark:active:bg-emerald-900/40',
              'disabled:opacity-60')}
            aria-label={`Resume session ${session.name}`}
            aria-busy={generating}
            disabled={generating}
            title="Resume — generate the next step with AI"
            onClick={() => void handleSparkle()}
          >
            {generating
              ? <Loader2 size={16} aria-hidden="true" className="animate-spin" />
              : <Sparkles size={16} aria-hidden="true" />}
          </button>
        )}
        {/* Secondary actions group — wide-card-only. Suggestions/rename/archive/
            delete each render a 44px tap target with no hover to retreat behind,
            so in a narrow card they consumed most of the card width. Gating the
            group to `@lg:` leaves Resume as the single inline action when narrow.
            `@lg:contents` dissolves this wrapper on a wide card so its buttons
            become direct children of the parent action grid (two rows), while
            staying `hidden` (as a box) when narrow. */}
        <div data-testid="card-secondary-actions" className="hidden @lg:contents">
        {!isBusy && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label={`Quick replies for ${session.name}`}
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
                {/* Dynamic section: the backend's cached context suggestions. The
                    sparkle generates fresh on tap, so none is "already sent" — show
                    them all here. Empty for most sessions. */}
                {suggestions.map((s, i) => (
                  <button
                    key={`suggestion-${i}`}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onQuickResume(session.id, s) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {s}
                  </button>
                ))}
                {suggestions.length > 0 && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {/* Canned section: hard-coded one-tap replies, always available. */}
                {QUICK_REPLIES.map((reply) => (
                  <button
                    key={reply}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onQuickResume(session.id, reply) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {reply}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {onToggleAutopilot && (
          <button
            type="button"
            data-testid="card-autopilot"
            className={cn(actionBtn,
              session.autopilotPaused
                ? 'text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700'
                : 'text-emerald-600 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-900/20',
              'focus-visible:ring-blue-500')}
            aria-label={session.autopilotPaused
              ? `Autopilot paused for session ${session.name} — resume`
              : `Autopilot on for session ${session.name} — pause`}
            aria-pressed={!session.autopilotPaused}
            title={session.autopilotPaused
              ? 'Autopilot paused — click to resume'
              : 'Autopilot on — click to pause'}
            onClick={() => onToggleAutopilot(session.id, !session.autopilotPaused)}
          >
            <Bot size={16} aria-hidden="true" />
          </button>
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
     </div>
      {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
      </div>
    </article>
  )
}
