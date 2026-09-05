import { useRef, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Sparkles, Loader2, Target, Wrench, Bot, MoreVertical } from 'lucide-react'
import { toast } from 'sonner'
import { inGroup, matchShortcut } from '@/shell/shortcuts'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'
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
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useSwipeAction } from '@/lib/useSwipeAction'
import { useNow } from '@/hooks/useNow'
import type { Session, Account } from '@/api/types'

// Shared sizing for the per-session icon buttons: 44px tap target on touch,
// a denser 32px on hover-capable (desktop) pointers.
const actionBtn = cn(
  'h-11 w-11 [@media(hover:hover)]:h-8 [@media(hover:hover)]:w-8',
  'flex items-center justify-center rounded-md flex-shrink-0',
  'focus-visible:outline-none focus-visible:ring-2',
  // Press feedback: a quick scale-down + background darken so a tap/click
  // is unmistakable (hover alone gives no change once the cursor is already
  // over the button, and touch has no hover at all). active: fires on both.
  'transition-[background-color,transform] duration-100',
  'active:scale-90 active:duration-75',
)

// Line-1 marker: a 12px icon with the full text demoted to tooltip + sr-only.
// Wide-card only (`hidden @md:inline-flex`), same declutter gate the old chips
// used — narrow cards keep the triage essentials and nothing else.
const markerBase = 'hidden @md:inline-flex items-center flex-shrink-0'

// Menu item styling shared by every entry of the unified ⋯ menu.
const menuItem = cn(
  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
  'hover:bg-gray-100 dark:hover:bg-gray-700',
  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-700',
)

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

// Neutral chip shell shared by the identity chips on line 1 (engine, reasoning
// effort, model). Each caps its own width and truncates so no single long value
// can starve the session title.
const identityChip = 'text-xs px-1.5 py-0.5 rounded truncate'

/** Passive `·` divider between line-2 segments. */
function Sep() {
  return <span aria-hidden="true" className="flex-shrink-0 text-gray-300 dark:text-gray-600">·</span>
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
  const articleRef = useRef<HTMLElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const [expanded, setExpanded] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuTriggerRef = useRef<HTMLButtonElement>(null)
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

  // Close the ⋯ menu on outside-click (mirrors SessionActions). Escape is the
  // `onKeyDown` on the menu's own container below — a component handling its own
  // key, not a second app-wide listener; PR 4 removed the `document` keydown that
  // used to live here.
  useEffect(() => {
    if (!menuOpen) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [menuOpen])

  // An open menu is an OVERLAY: it covers the rows under it, so the shell's
  // shortcuts (↑/↓ over the list, ← / Esc out of a session) must not fire
  // through it. One owner for that fact — `shell/useOverlayState` — the same one
  // `Dialog`, `Sheet` and `ui/Menu` register with.
  useEffect(() => {
    if (!menuOpen) return
    pushOverlay()
    return () => popOverlay()
  }, [menuOpen])

  // …and an open menu must OWN the keyboard, which means focus has to be inside
  // it. On macOS Safari and Firefox, clicking a `<button>` does not move focus to
  // it, and the browser delivers keydown to `document.activeElement` — so without
  // this the container's Escape handler below never fires for a mouse-opened
  // menu, while the overlay push above keeps every global shortcut suppressed.
  // The keyboard would go silently inert until the user clicked elsewhere.
  //
  // The TRIGGER, not the first item: it is what the markup names as the menu's
  // control (`aria-haspopup` / `aria-expanded`), the items carry no roving
  // tabindex to hand focus back to, and it stays mounted when the menu closes —
  // so nothing has to restore focus, and Tab walks straight into the menu, which
  // is the next thing in the DOM.
  useEffect(() => {
    if (menuOpen) menuTriggerRef.current?.focus()
  }, [menuOpen])

  const repoLabel = formatRepoLabel(session.repo)

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
    // The registry's key→action table for a focused ROW, through the one
    // selection rule the shell's listener uses (`matchShortcut`). No context is
    // passed: a focused row cannot honestly answer `hasOpenAction` or
    // `sessionOpen`, so a `list-mode` row that ever grows a `when` stops firing
    // here — loudly, in this component's tests — instead of being judged against
    // a fabricated context.
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

  /** Whole-card click-to-open. Interactive descendants (action buttons, PR
   *  links, the provenance link, open menus) keep their own behavior — a click
   *  that lands on or inside one is theirs, everything else opens the session. */
  function handleCardClick(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest('button, a, [role="menu"]')) return
    if (swipe.openSide) { swipe.reset(); return }   // tap closes a revealed card
    if (swipe.consumeDrag()) return                  // swallow the click that ends a drag
    onOpen(session.id)
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
      onClick={handleCardClick}
      className={cn(
        // `@container` makes the card a container-query context so the layout
        // below responds to the card's OWN width (i.e. the drag-resizable
        // sidebar), not the viewport. The gated variant must stay ≤ @md
        // (28rem/448px): queries resolve against this element's CONTENT box,
        // whose width tops out around 503px even at SIDEBAR_MAX, so @lg (512px)
        // can never match inside the sidebar and would hide the gated content
        // everywhere on desktop.
        'group @container relative flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px] cursor-pointer',
        'focus-within:ring-2 focus-within:ring-blue-500',
        selected && 'ring-2 ring-blue-500 bg-blue-50 dark:bg-blue-950/30',
        // Clip the swipe edge panels only while they are mounted (drag in flight
        // or a side held open). A permanent overflow-hidden on every sub-lg
        // viewport also clipped the ⋯ menu on wide touch devices (tablets).
        !isWide && (swipe.dragging || swipe.openSide !== null) && 'overflow-hidden',
      )}
    >
      {/* Status rail — the card's left edge always names the status tier
          (emerald working / amber needs-you / red error / gray done). It sits
          OUTSIDE the swipe foreground so triage color holds while swiping. */}
      <span
        data-testid="card-status-rail"
        aria-hidden="true"
        className={cn('absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full', accent.rail)}
      />

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

      {/* Foreground — the card body. On mobile it translates over the panels;
          on desktop it is a plain wrapper with no gesture handlers. */}
      <div
        {...(!isWide ? { 'data-testid': 'card-swipe-fg', onPointerDown: swipe.onPointerDown } : {})}
        style={(!isWide && restX !== 0) ? { transform: `translateX(${restX}px)` } : undefined}
        className={cn(
          !isWide && 'relative bg-white dark:bg-gray-800 touch-pan-y',
          !isWide && !swipe.dragging && !reduceMotion && 'transition-transform duration-200',
        )}
      >
        {/* The dense two-line skeleton: every element has ONE fixed slot, so a
            vertical scan reads the same on every card regardless of which
            optional signals a session carries.
            Line 1 (identity): dot · title · markers · PRs · usage · account · ✦ · ⋯
            Line 2 (context):  repo · host · status phrase — summary · last-active */}

        {/* Line 1 — identity. The open-session button wraps only passive content
            (dot, autopilot marker, title); every interactive chip is a sibling,
            so there are no nested-interactive violations. */}
        <div data-testid="card-identity" className="flex items-center gap-1.5 min-w-0">
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
              button because axe rejects it without role="option". */}
          <button
            type="button"
            id={`row-${session.id}`}
            ref={buttonRef}
            tabIndex={selected ? 0 : -1}
            data-selected={selected}
            className={cn(
              'flex-1 min-w-0 flex items-center gap-1.5 text-left py-0.5',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
            )}
            onClick={(e) => {
              // The article-level handler skips clicks on buttons — this IS the
              // open control, so it acts directly (with the same swipe guards).
              e.stopPropagation()
              if (swipe.openSide) { swipe.reset(); return }
              if (swipe.consumeDrag()) return
              onOpen(session.id)
            }}
            onKeyDown={handleKeyDown}
            aria-label={`Open session ${session.name}`}
          >
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
          </button>
          {/* Sub-agent count, visible without expanding. Only shown while
              collapsed — once expanded, the chevron + SubAgentRows carry it. */}
          {hasSubAgents && !expanded && (
            <span className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
              {subAgents.length} {subAgents.length === 1 ? 'agent' : 'agents'}
            </span>
          )}

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
          {/* The chip yields before the title does: capped at 30% of the row and
              truncating, so a long account email can't starve the session name.
              (Line 1 now also hosts the action buttons, so the old 45% cap left
              starved one-word titles.) `title` keeps the full value reachable. */}
          <span
            data-testid="card-account"
            className="flex-shrink-0 max-w-[30%] truncate text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded"
            title={account?.nickname || session.accountName}
          >
            {account?.nickname || session.accountName}
          </span>

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
          {/* Escape closes the menu here, on the container that holds both the
              trigger and the menu, so it works while focus is on either. It
              `stopPropagation`s so the shell's one keydown listener never sees
              the key — dismissing a menu must not also detach from a session. */}
          <div
            className="relative flex-shrink-0"
            ref={menuRef}
            onKeyDown={(e) => {
              if (menuOpen && e.key === 'Escape') {
                e.stopPropagation()
                setMenuOpen(false)
              }
            }}
          >
            <button
              type="button"
              ref={menuTriggerRef}
              className={cn(actionBtn, 'text-gray-500 dark:text-gray-400',
                'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:ring-blue-500',
                'active:bg-gray-200 dark:active:bg-gray-600')}
              aria-label={`More actions for session ${session.name}`}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              title="More actions"
              onClick={() => setMenuOpen((o) => !o)}
            >
              <MoreVertical size={16} aria-hidden="true" />
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
                {/* Reply sections — only for a resumable (non-busy) session.
                    Dynamic cached suggestions first, canned replies below. */}
                {!isBusy && suggestions.map((s, i) => (
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
                {!isBusy && suggestions.length > 0 && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {!isBusy && QUICK_REPLIES.map((reply) => (
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
                {!isBusy && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {/* Management section — always available, busy or not. */}
                {onToggleAutopilot && (
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="card-overflow-autopilot"
                    onClick={() => { setMenuOpen(false); onToggleAutopilot(session.id, !session.autopilotPaused) }}
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
                  onClick={() => { setMenuOpen(false); onRename(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Pencil size={16} aria-hidden="true" />
                  Rename
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); onClose(session.id) }}
                  className={cn(menuItem, 'text-gray-700 dark:text-gray-200')}
                >
                  <Archive size={16} aria-hidden="true" />
                  Archive
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); onDelete(session.id) }}
                  className={cn(menuItem, 'text-red-600 dark:text-red-400')}
                >
                  <Trash2 size={16} aria-hidden="true" />
                  Delete
                </button>
              </div>
            )}
          </div>
        </div>

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

        {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
      </div>
    </article>
  )
}
