import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Sparkles, Loader2, Target, Wrench, Bot } from 'lucide-react'
import { toast } from 'sonner'
import { inGroup, matchShortcut } from '@/shell/shortcuts'
import { QUICK_REPLIES } from '@/lib/quickReplies'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { bindingWindow } from '@/lib/usageWindows'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { TIER_ACCENT } from '@/session/statusTier'
import { sessionTriage } from '@/session/triageTier'
import { useSessionPromptFlight } from '@/lib/promptFlight'
import { anyChecksFailing, isActionablePrState } from '@/session/prStatus'
import { engineBadge, isGatedTier, tierLabel } from '@/session/modelName'
import { UsageBadge } from './UsageBadge'
import { PrList } from './PrList'
import { SubAgentRows } from './SubAgentRows'
import { CardFrame } from './card/CardFrame'
import { CardMenu, Sep, actionBtn, menuItem } from './card/CardMenu'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useNow } from '@/hooks/useNow'
import type { Session, Account } from '@/api/types'

// Meta-row marker: a 12px icon with the full text demoted to tooltip + sr-only.
// Wide-card only (`hidden @md:inline-flex`), same declutter gate the old chips
// used — narrow cards keep the triage essentials and nothing else.
const markerBase = 'hidden @md:inline-flex items-center flex-shrink-0'

// Human-readable labels for automation ids in the provenance marker — one map for
// spawned-by and assisted-by (the pr-doctor special case previously lived inline).
const AUTOMATION_LABELS: Record<string, string> = {
  'pr-doctor': 'PR Doctor',
  'backlog-runner': 'Backlog Runner',
  autopilot: 'Autopilot',
}

function automationLabel(id: string): string {
  return AUTOMATION_LABELS[id] ?? id
}

/**
 * Tooltip for the model chip: the current model plus whichever transitional
 * state applies. A queued after-turn switch (`modelPending`) and a parked
 * upgrade awaiting approval (`pendingModel`) are mutually exclusive on the
 * server — the former is an already-approved switch, the latter is the hold.
 */
function modelTitle(s: Pick<Session, 'model' | 'modelPending' | 'pendingModel' | 'engine'>): string {
  const label = `Model: ${tierLabel(s.model, s.engine)}`
  if (s.pendingModel) return `${label} (${tierLabel(s.pendingModel, s.engine)} requested, awaiting approval)`
  if (s.modelPending) return `${label} (switching after this turn)`
  return label
}

// Neutral chip shell shared by the identity chips on the meta row (engine,
// reasoning effort, model). Each caps its own width and truncates so no single
// long value can push the rest of the row off the card.
const identityChip = 'text-xs px-1.5 py-0.5 rounded truncate'

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
   *  strings; the card sends the top one. Hard-coded replies stay in the menu only. */
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
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const [expanded, setExpanded] = useState(false)
  const [generating, setGenerating] = useState(false)

  const suggestions = session.latestSuggestions ?? []
  // One triage read drives everything status-colored on the card: the rail, the
  // dot and the line-2 phrase all read the same tier, so they can never disagree
  // (the restart-stranded relabel included). sessionTriage is the SAME function
  // the group header / sort use, so a card can never show a calm "Waiting…"/"Your
  // turn" pill while sitting under the "Failed" header (Fix B).
  // A prompt this user sent whose turn has not started yet. Read from the socket-owned
  // store rather than from the session record: the wait happens entirely before the
  // server has any new status to report, which is exactly why the card used to sit on
  // a calm "Ready" through the whole thing.
  const promptStage = useSessionPromptFlight(session.id)
  const presentation = sessionTriage(session, promptStage)
  const accent = TIER_ACCENT[presentation.tier]
  const isBusy = presentation.tier === 'busy'

  // "Guided by autopilot": the robot has acted on this session (assistedBy) AND it
  // isn't pinned to manual. The pause veto matters — a session can carry the
  // autopilot provenance yet be paused now, so a green "being driven" signal would
  // contradict the grey "Manual" marker. autopilotLive narrows that to "acting right
  // now" (busy turn), which is what earns the pulse.
  const guidedByAutopilot = session.assistedBy === 'autopilot' && !session.autopilotPaused
  const autopilotLive = guidedByAutopilot && isBusy

  // Sparkle (✨) tap ALWAYS asks the LLM for a fresh next-prompt, then sends the
  // top result — never a cached value or a hard-coded fallback (those live only in
  // the menu). The backend lazily resumes a detached session before delivering.
  // When the model returns nothing we surface a hint instead of silently sending
  // "continue"; the menu's canned replies are the manual escape hatch.
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

  const now = useNow()

  // Autopilot human-deference countdown: show while the server-supplied cooldown instant is still
  // in the future. `now === 0` is the pre-first-tick frame (useNow returns 0 until it ticks) — show
  // the marker then too so there's no mount flash; the minutes label fills in on the first tick.
  const cooldownUntilMs = session.autopilotCooldownUntil ? Date.parse(session.autopilotCooldownUntil) : null
  const showCooldown = cooldownUntilMs !== null && (now === 0 || now < cooldownUntilMs)
  const cooldownMinsLeft = cooldownUntilMs !== null && now > 0
    ? Math.max(1, Math.ceil((cooldownUntilMs - now) / 60000))
    : null

  const repoLabel = formatRepoLabel(session.repo)

  // The dot pulse and the autopilot marker degrade to static glyphs under
  // reduced-motion. (CardFrame reads the same query for the swipe transition;
  // a second call to one media query is free.)
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')

  function handleKeyDown(e: React.KeyboardEvent) {
    // The registry's key→action table for a focused ROW, through the one
    // selection rule the shell's listener uses (`matchShortcut`). No context is
    // passed: a focused row cannot honestly answer `hasOpenAction` or
    // `sessionOpen`, so a `list-mode` row that ever grows a `when` stops firing
    // here — loudly, in this component's tests — instead of being judged against
    // a fabricated context. CardFrame only calls this in list mode; outside it
    // the frame keeps the Enter/Space "open" affordance and nothing else, so a
    // natively-focused row can't fire a stray destructive key.
    const action = matchShortcut(e, inGroup('list-mode'))?.action
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

  // The dense three-line skeleton: every element has ONE fixed slot, so a
  // vertical scan reads the same on every card regardless of which optional
  // signals a session carries.
  //   Line 1 (identity): dot · title · ✦ · ⋯
  //   Line 2 (context):  repo · host · status phrase — summary · last-active
  //   Line 3 (meta):     markers · PRs · usage · engine · model · account
  //
  // Line 1 is a flex row whose only growable child is the title, so the title
  // takes the row's whole remainder. It used to share the row with the chip
  // cluster — all of it flex-shrink-0 — which meant the title absorbed 100% of
  // the width deficit and rendered as "I…" on a ~500px card. Nothing that can
  // grow may move back onto it (SessionCard.layout.test.tsx pins this).
  //
  // `identity` is the passive content INSIDE the open button; every interactive
  // chip is a `trailing` sibling or lives on line 3, so there are no
  // nested-interactive violations.
  return (
    <CardFrame
      id={session.id}
      openLabel={`Open session ${session.name}`}
      railClass={accent.rail}
      selected={selected}
      listMode={listMode}
      onOpen={onOpen}
      onKeyDown={handleKeyDown}
      swipeEnabled={!session.pending}
      swipeRight={{
        label: `Archive ${session.name}`,
        icon: <Archive size={20} aria-hidden="true" />,
        onCommit: () => onClose(session.id),
        testId: 'swipe-panel-archive',
        className: 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
      }}
      swipeLeft={{
        label: `Delete ${session.name}`,
        icon: <Trash2 size={20} aria-hidden="true" />,
        onCommit: () => onDelete(session.id),
        testId: 'swipe-panel-delete',
        className: 'bg-red-600 text-white',
      }}
      leading={hasSubAgents ? (
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
      ) : undefined}
      identity={
        <>
          <span
            data-testid="card-status-dot"
            aria-hidden="true"
            className={cn(
              'h-2 w-2 rounded-full flex-shrink-0',
              accent.dot,
              accent.pulse && !reduceMotion && 'motion-safe:animate-pulse',
            )}
          />
          {/* Autopilot marker — non-interactive, safe inside the open button.
              Pulses only while the robot is actively working (autopilotLive),
              and degrades to a static glyph under reduced-motion. */}
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
            className="min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            title={session.nameIsAuto ? 'Auto-named by Claude' : undefined}
          >
            {session.name}
          </span>
        </>
      }
      trailing={
        <>
          {/* Sub-agent count, visible without expanding. Only shown while
              collapsed — once expanded, the chevron + SubAgentRows carry it. */}
          {hasSubAgents && !expanded && (
            <span className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
              {subAgents.length} {subAgents.length === 1 ? 'agent' : 'agents'}
            </span>
          )}

          {/* Actions — two permanent slots, identical on every device: Resume (✦)
              and the unified ⋯ menu. Nothing appears or disappears on hover, so
              the card's right edge is static. */}
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
          <CardMenu label={`More actions for session ${session.name}`}>
            {(close) => (
              <>
                {/* Reply sections — only for a resumable (non-busy) session.
                    Dynamic cached suggestions first, canned replies below. */}
                {!isBusy && suggestions.map((s, i) => (
                  <button
                    key={`suggestion-${i}`}
                    type="button"
                    role="menuitem"
                    onClick={() => { close(); onQuickResume(session.id, s) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {s}
                  </button>
                ))}
                {!isBusy && suggestions.length > 0 && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {!isBusy && QUICK_REPLIES.map((reply) => (
                  <button
                    key={reply}
                    type="button"
                    role="menuitem"
                    onClick={() => { close(); onQuickResume(session.id, reply) }}
                    className={cn('block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700')}
                  >
                    {reply}
                  </button>
                ))}
                {!isBusy && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {/* Management section — always available, busy or not. */}
                {onToggleAutopilot && (
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="card-overflow-autopilot"
                    onClick={() => { close(); onToggleAutopilot(session.id, !session.autopilotPaused) }}
                    className={cn(menuItem,
                      session.autopilotPaused
                        ? 'text-gray-700 dark:text-gray-200'
                        : 'text-emerald-700 dark:text-emerald-300')}
                  >
                    <Bot size={16} aria-hidden="true" />
                    {session.autopilotPaused ? 'Resume autopilot' : 'Pause autopilot'}
                  </button>
                )}
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onRename(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Pencil size={16} aria-hidden="true" />
                  Rename
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onClose(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Archive size={16} aria-hidden="true" />
                  Archive
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onDelete(session.id) }}
                  className={cn(menuItem, 'text-red-600 dark:text-red-400')}
                >
                  <Trash2 size={16} aria-hidden="true" />
                  Delete
                </button>
              </>
            )}
          </CardMenu>
        </>
      }
    >
      {/* Line 2 — context narrative: repo · host · status phrase — summary,
          with last-active pinned right. All passive; a click here falls
          through to the article's open-session handler. */}
      <div data-testid="card-context" className="mt-0.5 flex items-center gap-1.5 min-w-0 text-xs">
        <span
          className={cn(
            'min-w-0 inline-flex items-center gap-1',
            repoLabel.isScratch
              ? 'text-gray-400 dark:text-gray-500 italic'
              : 'text-gray-600 dark:text-gray-300',
          )}
          title={repoLabel.text}
        >
          <FolderGit2 size={12} aria-hidden="true" className="flex-shrink-0" />
          <span className="truncate">{repoLabel.text}</span>
        </span>
        {session.host && (
          <>
            <Sep />
            <span
              data-testid="card-host"
              className="flex-shrink-0 text-gray-500 dark:text-gray-400"
              title={`host ${session.host}`}
            >
              {session.host}
            </span>
          </>
        )}
        <Sep />
        {/* The status phrase is the labeled, always-visible triage read; the
            rail + dot carry the same tier as color. */}
        <span
          aria-label={`Status: ${presentation.label}`}
          className={cn('flex-shrink-0 font-medium', presentation.textClass)}
        >
          {presentation.label}
        </span>
        {presentation.tier === 'unreachable' && isValidTimestamp(session.lastConfirmedAliveAt) && (
          <span className="flex-shrink-0 text-slate-500 dark:text-slate-400">
            {' '}— last seen {relTime(session.lastConfirmedAliveAt, now || undefined)}
          </span>
        )}
        {session.summary && (
          <span
            data-testid="card-summary"
            className="min-w-0 flex-1 truncate text-gray-500 dark:text-gray-400"
            title={session.summary}
          >
            — {session.summary}
          </span>
        )}
        {/* Last activity — helps tell live sessions from stale ones at a
            glance. Guarded against a missing/invalid timestamp so we never
            show "NaN" or the 1970 epoch. Suppressed while busy: `last_turn_at`
            (its source) freezes for the whole in-progress turn, so it would
            read "active 10m ago" on a session that is actively streaming. */}
        {!isBusy && isValidTimestamp(session.updatedAt) && (
          <span
            className="ml-auto flex-shrink-0 text-gray-400 dark:text-gray-500 tabular-nums"
            title={`Last active ${new Date(session.updatedAt).toLocaleString()}`}
          >
            active {relTime(session.updatedAt, now || undefined)}
          </span>
        )}
      </div>

      {/* Line 3 — meta: every chip that can grow, off the title's row. The
          @md declutter gates are unchanged; only the row they sit on is. All
          passive except the provenance link and Fix, so a click elsewhere
          falls through to the article's open-session handler. */}
      <div data-testid="card-meta" className="mt-1 flex items-center gap-1.5 min-w-0">
        {/* Marker cluster — secondary state demoted to quiet 12px icons. Each
            keeps its old testid and full text (tooltip + sr-only), so nothing
            is lost, it just stops shouting. */}
        {session.activeGoal && (
          <span
            data-testid="card-goal"
            className={cn(markerBase, 'text-indigo-600 dark:text-indigo-400')}
            title={`Goal: ${session.activeGoal}`}
          >
            <Target size={12} aria-hidden="true" />
            <span className="sr-only">Goal</span>
          </span>
        )}
        {!session.activeGoal && session.goalSource === 'inferred' && session.effectiveGoal && (
          /* Subdued variant: the goal is LLM-inferred, not user-set. */
          <span
            data-testid="card-goal-inferred"
            className={cn(markerBase, 'text-indigo-400 dark:text-indigo-500')}
            title={`Goal (inferred): ${session.effectiveGoal}`}
          >
            <Target size={12} aria-hidden="true" />
            <span className="sr-only">Goal</span>
          </span>
        )}
        {session.autopilotPaused && (
          <span
            data-testid="card-autopilot-badge"
            className={cn(markerBase, 'text-slate-400 dark:text-slate-500')}
            title="Autopilot paused — this session is on manual"
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">Manual</span>
          </span>
        )}
        {showCooldown && (
          <span
            data-testid="card-cooldown-badge"
            className={cn(markerBase, 'text-amber-600 dark:text-amber-400')}
            title={`Autopilot is standing down while you steer this session${cooldownMinsLeft ? ` (${cooldownMinsLeft}m left)` : ''}`}
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">
              Autopilot paused — you're steering{cooldownMinsLeft ? ` (${cooldownMinsLeft}m left)` : ''}
            </span>
          </span>
        )}
        {session.autoArchiveOptOut && (
          /* Passive indicator: this session was manually unarchived, so
             autopilot will never auto-archive it again. */
          <span
            data-testid="card-no-autoarchive-badge"
            className={cn(markerBase, 'text-amber-600 dark:text-amber-400')}
            title="Auto-archive off — you unarchived this session, so autopilot won't archive it again"
          >
            <Archive size={12} aria-hidden="true" />
            <span className="sr-only">Auto-archive off</span>
          </span>
        )}
        {(session.origin || session.assistedBy) && (
          /* Provenance: one marker for any automation involvement.
             Spawned-by (origin) outranks assisted-by when both apply. */
          <Link
            to="/automation"
            data-testid="card-provenance"
            className={cn(
              markerBase,
              'rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              // Emerald when autopilot is actively guiding (matches the title
              // marker); generic violet for created-by/other automation, and
              // for an autopilot session that's paused.
              guidedByAutopilot && !session.origin
                ? 'text-emerald-600 dark:text-emerald-400 hover:text-emerald-700 dark:hover:text-emerald-300'
                : 'text-violet-500 dark:text-violet-400 hover:text-violet-700 dark:hover:text-violet-300',
            )}
            title={session.origin
              ? `Created by automation: ${session.origin}`
              : `Assisted by automation: ${session.assistedBy}`}
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">
              {session.origin
                ? `Created by ${automationLabel(session.origin)}`
                : `Assisted by ${automationLabel(session.assistedBy!)}`}
            </span>
          </Link>
        )}
        {(session.prs?.length ?? 0) > 0 && (
          <span data-testid="card-prs" className="hidden @md:inline-flex items-center flex-shrink-0">
            <PrList prs={session.prs!} />
          </span>
        )}
        {showFix && failingPr && (
          <button
            type="button"
            data-testid="card-fix-pr"
            onClick={() => onFixPr?.(session.id, failingPr.number)}
            aria-label={`Fix PR #${failingPr.number}`}
            className="hidden @md:inline-flex items-center gap-1 flex-shrink-0 text-xs font-medium px-1.5 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
          >
            <Wrench size={11} aria-hidden />
            <span>Fix</span>
          </button>
        )}
        {/* The card has room for ONE number, so show the window that actually
            binds this account (the most-used one) rather than assuming a
            dimension named "session" that a single-allowance provider does not
            have. */}
        {(() => {
          const w = bindingWindow(account?.usage)
          return w ? (
            <span data-testid="card-usage" className="hidden @md:inline-flex items-center flex-shrink-0">
              <UsageBadge
                compact
                className="inline-flex"
                pct={w.pct}
                label={w.label}
                resetsAt={w.resetsAt}
                staleAsOf={account?.usage?.staleAsOf}
              />
            </span>
          ) : null
        })()}
        {/* Agent chip — WHICH engine runs this session. Only rendered when it
            is not the default Claude Code engine (`engineBadge` owns that
            rule): a deployment that runs only Claude must not grow a chip on
            every card. Wide-card only, like every other secondary signal. */}
        {engineBadge(session.engine) && (
          <span
            data-testid="card-engine"
            className={cn(
              markerBase, identityChip, 'max-w-[6rem]',
              'bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300',
            )}
            title={`Agent: ${engineBadge(session.engine)}`}
          >
            {engineBadge(session.engine)}
          </span>
        )}
        {/* Reasoning effort — the engine-native depth choice (Codex). Absent
            means "the model's own default", the unremarkable case. It sits
            BEFORE the model chip because the model chip must stay adjacent to
            the account chip (see the comment below). */}
        {session.reasoningEffort && (
          <span
            data-testid="card-effort"
            className={cn(
              markerBase, identityChip, 'max-w-[6rem]',
              'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400',
            )}
            title={`Reasoning effort: ${session.reasoningEffort}`}
          >
            {session.reasoningEffort}
          </span>
        )}
        {/* Model chip — which model the session is running, right before the
            account chip so the pair reads together ("Opus · tony@…"). Only
            shown when the record pins a model; null means the account/binary
            default, which is the unremarkable case and would just be noise on
            most cards. Wide-card only, like every other secondary signal. The
            gated tier is tinted so an expensive session stands out on a scan
            — never for a non-Claude engine, whose ids no tier gate matches. */}
        {session.model && (
          <span
            data-testid="card-model"
            className={cn(
              markerBase, identityChip, 'max-w-[8rem]',
              isGatedTier(session.model, session.engine)
                ? 'bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400',
            )}
            title={modelTitle(session)}
          >
            {tierLabel(session.model, session.engine)}
          </span>
        )}
        {/* The 30% cap existed solely to stop a long account email starving the
            session name. There is no title on this row to protect, and 30%
            clipped an ordinary gmail address — so the cap widens to 50%. It
            stays capped and truncating for genuinely long aliases, and `title`
            keeps the full value reachable either way.

            It is also the ONE shrinkable child of this row (`min-w-0`, no
            `flex-shrink-0`), which is what stops line 3 repeating line 1's bug
            in reverse: with every child fixed-width, a full chip set would
            overflow the card instead of yielding. The account chip is the right
            one to yield — it is identical on every card of a single-account
            deployment, so it is the least discriminating thing on the row. The
            row must NOT take `overflow-hidden` instead: PrList opens an
            `absolute z-20` popover out of it (same trap CardFrame records for
            the ⋯ menu). */}
        <span
          data-testid="card-account"
          className="min-w-0 max-w-[50%] truncate text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded"
          title={account?.nickname || session.accountName}
        >
          {account?.nickname || session.accountName}
        </span>
      </div>

      {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
    </CardFrame>
  )
}
