import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Pencil, Archive, Trash2, FolderGit2, ChevronRight, ChevronDown, Sparkles, Loader2, Target, Wrench, Bot, Monitor } from 'lucide-react'
import { toast } from 'sonner'
import { inGroup, matchShortcut } from '@/shell/shortcuts'
import { QUICK_REPLIES } from '@/lib/quickReplies'
import { cn } from '@/lib/utils'
import { formatRepoLabel } from '@/lib/repoLabel'
import { bindingWindow } from '@/lib/usageWindows'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { TIER_ACCENT, type StatusTier } from '@/session/statusTier'
import { sessionTriage } from '@/session/triageTier'
import { useSessionPromptFlight } from '@/lib/promptFlight'
import { anyChecksFailing, isActionablePrState } from '@/session/prStatus'
import { engineBadge, isGatedTier, tierLabel } from '@/session/modelName'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { UsageBadge } from '@/features/accounts/UsageBadge'
import { PrList } from './PrList'
import { SubAgentRows } from './SubAgentRows'
import { ChildSessionRows } from './ChildSessionRows'
import { BackgroundWorkRows } from './BackgroundWorkRows'
import { selectBackgroundWork } from '@/session/selectors'
import { backgroundWorkSummary } from '@/features/sessions/backgroundWorkSummary'
import { CardFrame } from '@/ui/list/CardFrame'
import { CardMenu, Sep, actionBtn, menuItem } from '@/ui/list/CardMenu'
import { useViewport } from '@/shell/useViewport'
import { useNow } from '@/hooks/useNow'
import type { Session, Account } from '@/api/types'

// Meta-row marker: a 12px icon with the full text demoted to tooltip + sr-only.
// Wide-card only (`hidden @md:inline-flex`), same declutter gate the old chips
// used — narrow cards keep the triage essentials and nothing else.
const markerBase = 'hidden @md:inline-flex items-center flex-shrink-0'

// Tiers at which the card offers no way to SEND (✦, quick replies, `c`): a live
// turn, a session parked in the background (an armed Monitor/wakeup, or parked
// on usage — `waiting_external`/`queued`/usage-stalled), and a host that cannot
// honour a send. Everything else — calm, your-turn, needs-you, failed, complete
// — may be resumed. See `canResume` below.
const NO_RESUME_TIERS: ReadonlySet<StatusTier> = new Set<StatusTier>(['busy', 'waiting', 'unreachable'])

// Provenance labels come from the server (`originLabel` / `assistedByLabel`,
// the automation registry's display names) — the card keeps no id→name map of
// its own. The id is the fallback for a payload from an older server.

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
  /** Called when the user toggles this session's auto-archive opt-out — parent owns the mutation. */
  onToggleAutoArchive?: (id: string, optedOut: boolean) => void
  /** Called when the user answers a "likely complete" hand-back: confirm archives the
   *  session, reject clears the hint — parent owns the mutation. */
  onResolveSuspectedComplete?: (id: string, decision: 'confirm' | 'reject') => void
  /** Sessions this one dispatched (child-session spec §4.6); derived by the list from the
   *  same sessions array, so the card never fetches. */
  childSessions?: Session[]
  /** Name of the session that dispatched this one, for the "child of" pill; null when
   *  the parent is not in the loaded list. */
  parentName?: string | null
}

export function SessionCard({ session, account, selected = false, listMode = false, onOpen, onRename, onClose, onDelete, onQuickResume, onGenerateSuggestion, onNavigate, onExit, prAutofixMode, onFixPr, onToggleAutopilot, onToggleAutoArchive, onResolveSuspectedComplete, childSessions = [], parentName = null }: SessionCardProps) {
  const subAgents = session.subAgents ?? []
  const hasSubAgents = subAgents.length > 0
  const hasChildren = childSessions.length > 0
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
  // Whether ✦, the quick replies and the `c` shortcut may SEND into this session.
  // Gated on the tier, not on `busy` alone (2026-09-29 re-critique, P1): a
  // session parked in the background (`waiting` — an armed Monitor/wakeup, or
  // parked on usage) is deliberately not taking input, and an `unreachable`
  // one sits on a host that cannot honour a send. Management (⋯) stays.
  const canResume = !NO_RESUME_TIERS.has(presentation.tier)
  // The session's own one-line ask, shown only while it IS handed back (#2321).
  // Status-gated rather than field-gated for the same reason prResolution is: the
  // column survives a resume, so the field alone would strand a stale ask on a
  // session that has since moved on.
  const handBackReason = session.status === 'handed_back' ? session.handBackReason : null
  // The suspected-complete overlay: the server's own reading that this hand-back
  // was a status report on a met objective. Same status gate as the reason — a
  // resumed session keeps its old row value until the next status write clears it.
  const suspectedComplete = session.status === 'handed_back' ? session.suspectedCompleteReason : null

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
    // `canResume` here too: the list-mode `c` shortcut reaches this directly.
    if (!onGenerateSuggestion || generating || !canResume) return
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

  // Background shells/Monitors/wakeup a parked session waits on (#2635). Ticks with the card's
  // shared clock; `now === 0` is the pre-first-tick frame, so fall back to the mount instant.
  const [mountedAt] = useState(Date.now)
  const backgroundWork = selectBackgroundWork([], {}, session.backgroundWork)
  const bgSummary = backgroundWorkSummary(backgroundWork, now || mountedAt)
  const hasBackground = bgSummary != null
  // One chevron expands both nested lists (sub-agent threads and child sessions), and
  // says which it holds so a screen reader is not told "sub-agents" for a children-only card.
  const hasNested = hasSubAgents || hasChildren || hasBackground
  const nestedNoun = ['sub-agents', 'child sessions', 'background work']
    .filter((_, i) => [hasSubAgents, hasChildren, hasBackground][i])
    .join(' and ')

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
  // reduced-motion. (CardFrame reads the same `reducedMotion` field for the
  // swipe transition — one subscription in useViewport, two readers.)
  const { reducedMotion: reduceMotion, coarsePointer } = useViewport()

  // The reply rows: cached suggestions first, canned replies below. One
  // definition for both shapes of the reply menu (the desktop chevron, the
  // touch ✦), so the two can never offer different sets.
  const replyRows = (close: () => void) => (
    <>
      {suggestions.map((s, i) => (
        <button
          key={`suggestion-${i}`}
          type="button"
          role="menuitem"
          onClick={() => { close(); onQuickResume(session.id, s) }}
          className={cn(menuItem, 'text-ink')}
        >
          <span className="truncate">{s}</span>
        </button>
      ))}
      {suggestions.length > 0 && (
        <div role="separator" className="my-1 border-t border-edge" />
      )}
      {QUICK_REPLIES.map((reply) => (
        <button
          key={reply}
          type="button"
          role="menuitem"
          onClick={() => { close(); onQuickResume(session.id, reply) }}
          className={cn(menuItem, 'text-ink')}
        >
          {reply}
        </button>
      ))}
    </>
  )
  const sparkleGlyph = generating
    ? <Loader2 size={16} aria-hidden="true" className="animate-spin" />
    : <Sparkles size={16} aria-hidden="true" />

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
          'group flex flex-col rounded-lg border border-edge',
          'bg-surface px-3 py-2 min-h-target',
          'opacity-70 cursor-default select-none',
        )}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="inline-block h-3 w-3 flex-shrink-0 animate-spin rounded-full border-2 border-edge-strong border-t-transparent"
            aria-hidden="true"
          />
          <span className="flex-1 min-w-0 truncate text-sm font-semibold text-ink">
            {session.name}
          </span>
        </div>
        <p className="text-xs text-ink-muted mt-1">Starting…</p>
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
        className: 'bg-surface-raised text-ink',
      }}
      swipeLeft={{
        label: `Delete ${session.name}`,
        icon: <Trash2 size={20} aria-hidden="true" />,
        onCommit: () => onDelete(session.id),
        testId: 'swipe-panel-delete',
        className: 'bg-danger-strong text-on-strong',
      }}
      leading={hasNested ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? `Hide ${nestedNoun}` : `Show ${nestedNoun}`}
          aria-expanded={expanded}
          className={cn(
            'flex-shrink-0 h-6 w-6 flex items-center justify-center rounded',
            'text-ink-muted hover:text-ink',
            'hover:bg-surface-raised',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
                'flex-shrink-0 inline-flex items-center text-success',
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
          {/* Maintenance chip — the one provenance marker that is NOT demoted
              to an icon on line 3. A maintenance session is only in the Active
              list because it needs a human, and the reader has to see at a
              glance that "PR Doctor" is asking, not their own work; in the
              Background scope it says which automation the row belongs to.
              Non-interactive (the line-3 marker is the link), fixed width so
              the title still owns the row's remainder. */}
          {session.workClass === 'maintenance' && (
            <span
              data-testid="card-maintenance-chip"
              className={cn(
                'flex-shrink-0 max-w-[9rem] truncate rounded px-1.5 py-0.5 text-2xs font-medium leading-4',
                'bg-info-wash text-info',
              )}
              title={`Maintenance session created by ${session.originLabel ?? session.origin ?? 'an automation'}`}
            >
              {session.originLabel ?? session.origin ?? 'Maintenance'}
            </span>
          )}
          {/* Lineage pill (child-session spec §4.6): this session was dispatched by
              another session's model. Opens the parent. */}
          {session.parentSessionId && (
            <button
              type="button"
              data-testid="card-parent-pill"
              onClick={(e) => { e.stopPropagation(); onOpen(session.parentSessionId!) }}
              className={cn(
                'flex-shrink-0 max-w-[10rem] truncate rounded px-1.5 py-0.5 text-2xs font-medium leading-4',
                'bg-info-wash text-info hover:underline',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
              title="Open the session that dispatched this one"
            >
              child of {parentName ?? session.parentSessionId.slice(0, 8)}
            </button>
          )}
          <span
            className="min-w-0 truncate text-sm font-semibold text-ink"
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
            <span className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-info-wash text-info">
              {subAgents.length} {subAgents.length === 1 ? 'agent' : 'agents'}
            </span>
          )}
          {bgSummary && !expanded && (
            <span data-testid="card-background-pill" className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-info-wash text-info">{bgSummary}</span>
          )}
          {hasChildren && !expanded && (
            <span className="flex-shrink-0 text-xs tabular-nums px-1.5 py-0.5 rounded bg-info-wash text-info">
              {childSessions.length} {childSessions.length === 1 ? 'child' : 'children'}
            </span>
          )}

          {/* Actions — permanent slots, identical on every device: Resume (✦) with
              its quick-reply chevron, and the ⋯ management menu. Nothing appears
              or disappears on hover, so the card's right edge is static.

              Two menus on purpose (2026-09-29 critique, P1): the unified ⋯ held
              one-tap SENDS (four cached suggestions, four canned replies) in the
              same 13-row list as Rename, Archive and Delete, at 36px a row. A
              reply is a send — the composer already keeps those behind ✦'s own
              chevron, so the card does the same, and ⋯ is management only. */}
          {canResume && coarsePointer && (
            // Touch: a bare ✦ is 44px of one-tap send 8px from the title tap, so
            // here ✦ IS the menu — "Generate next step" first, then the replies.
            // A send is always a second, named tap.
            <CardMenu
              label={`Resume session ${session.name}`}
              title="Resume — pick what to send"
              icon={sparkleGlyph}
              triggerClassName={cn('text-success',
                'hover:bg-success-wash focus-visible:ring-success active:bg-success-wash')}
            >
              {(close) => (
                <>
                  <div role="presentation" className="px-3 pb-1 pt-1.5 text-2xs uppercase tracking-wide text-ink-muted">
                    Send a reply
                  </div>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={generating}
                    onClick={() => { close(); void handleSparkle() }}
                    className={cn(menuItem, 'font-medium text-success')}
                  >
                    <Sparkles size={16} aria-hidden="true" />
                    Generate next step
                  </button>
                  <div role="separator" className="my-1 border-t border-edge" />
                  {replyRows(close)}
                </>
              )}
            </CardMenu>
          )}
          {canResume && !coarsePointer && (
            // flex-shrink-0: line 1's only flexing child is the open control, so
            // the title takes the row's remainder (SessionCard.layout.test).
            <div className="flex flex-shrink-0 items-center">
              <button
                type="button"
                className={cn(actionBtn, 'text-success',
                  'hover:bg-success-wash focus-visible:ring-success',
                  'active:bg-success-wash',
                  'disabled:opacity-60')}
                aria-label={`Resume session ${session.name}`}
                aria-busy={generating}
                disabled={generating}
                title="Resume — generate the next step with AI"
                onClick={() => void handleSparkle()}
              >
                {sparkleGlyph}
              </button>
              <CardMenu
                label={`Quick replies for session ${session.name}`}
                title="Quick replies — one tap sends"
                icon={<ChevronDown size={14} aria-hidden="true" />}
                triggerClassName={cn('w-7 [@media(hover:hover)]:w-5 text-success',
                  'hover:bg-success-wash focus-visible:ring-success active:bg-success-wash')}
              >
                {(close) => (
                  <>
                    {/* Name the consequence: every row below is a send. */}
                    <div role="presentation" className="px-3 pb-1 pt-1.5 text-2xs uppercase tracking-wide text-ink-muted">
                      Send a reply
                    </div>
                    {replyRows(close)}
                  </>
                )}
              </CardMenu>
            </div>
          )}
          <CardMenu label={`More actions for session ${session.name}`}>
            {(close) => (
              <>
                {/* Management only — always available, busy or not. */}
                {onToggleAutopilot && (
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="card-overflow-autopilot"
                    onClick={() => { close(); onToggleAutopilot(session.id, !session.autopilotPaused) }}
                    className={cn(menuItem,
                      session.autopilotPaused
                        ? 'text-ink'
                        : 'text-success')}
                  >
                    <Bot size={16} aria-hidden="true" />
                    {session.autopilotPaused ? 'Resume autopilot' : 'Pause autopilot'}
                  </button>
                )}
                {onToggleAutoArchive && (
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="card-overflow-auto-archive"
                    onClick={() => { close(); onToggleAutoArchive(session.id, !session.autoArchiveOptOut) }}
                    className={cn(menuItem,
                      session.autoArchiveOptOut
                        ? 'text-ink'
                        : 'text-success')}
                  >
                    <Archive size={16} aria-hidden="true" />
                    {session.autoArchiveOptOut ? 'Enable auto-archive' : 'Disable auto-archive'}
                  </button>
                )}
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onRename(session.id) }}
                  className={cn(menuItem, 'text-ink')}
                >
                  <Pencil size={16} aria-hidden="true" />
                  Rename
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onClose(session.id) }}
                  className={cn(menuItem, 'text-ink')}
                >
                  <Archive size={16} aria-hidden="true" />
                  Archive
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { close(); onDelete(session.id) }}
                  className={cn(menuItem, 'text-danger')}
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
          with last-active pinned right. Passive except the conditional "See
          capacity" link; everywhere else a click falls through to the
          article's open-session handler. */}
      <div data-testid="card-context" className="mt-0.5 flex items-center gap-1.5 min-w-0 text-xs">
        <span
          className={cn(
            'min-w-0 inline-flex items-center gap-1',
            repoLabel.isScratch
              ? 'text-ink-muted italic'
              : 'text-ink-muted',
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
              className="flex-shrink-0 text-ink-muted"
              title={`host ${session.host}`}
            >
              {session.host}
            </span>
          </>
        )}
        {session.displayMode === 'native' && (
          <>
            <Sep />
            <span data-testid="card-desktop" className="flex-shrink-0 inline-flex items-center gap-1 text-ink-muted"
                  title="Windows open on the host's desktop">
              <Monitor size={12} aria-hidden="true" />
              <span>desktop</span>
            </span>
          </>
        )}
        <Sep />
        {/* The status phrase is the labeled, always-visible triage read; the
            rail + dot carry the same tier as color. */}
        <span
          aria-label={`Status: ${presentation.label}`}
          title={presentation.tier === 'waiting' && bgSummary ? `Waiting on ${bgSummary}` : undefined}
          className={cn('flex-shrink-0 font-medium', presentation.textClass)}
        >
          {presentation.label}
        </span>
        {/* C6: a session parked for lack of usage headroom is a capacity fact —
            link the stack rather than leave the pill as a dead end. Gated on
            the PRESENTATION tier, not the raw field: usageStalled only maps to
            a "Waiting for capacity" pill (tier 'waiting') when liveness is
            fresh and no restart/prompt-flight override applies — every other
            override (e.g. a stale-liveness "Unreachable") yields a different
            tier, and the link must not float free of the pill it explains. */}
        {session.usageStalled && presentation.tier === 'waiting' && (
          <Link
            to={deliveryPath('capacity')}
            className="flex-shrink-0 text-accent hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            See capacity
          </Link>
        )}
        {/* Why a finished session has no PR. Gated on the 'complete' tier (Complete /
            Ended), not on the field alone: a resumed session keeps its old row value,
            and "not-needed" beside "Working…" would be a stale claim. Without this a
            queued session the drainer ended as obsolete read as a bare "Ended" over
            "No messages yet" — a session that died, not one that was never needed. */}
        {presentation.tier === 'complete' && session.prResolution && (
          <span
            data-testid="card-resolution"
            className="min-w-0 truncate text-ink-muted"
            title={session.prResolution}
          >
            {' '}— {session.prResolution}
          </span>
        )}
        {presentation.tier === 'unreachable' && isValidTimestamp(session.lastConfirmedAliveAt) && (
          <span className="flex-shrink-0 text-ink-muted">
            {' '}— last seen {relTime(session.lastConfirmedAliveAt, now || undefined)}
          </span>
        )}
        {/* What a handed-back session actually wants (#2321). "Handed back" says
            something is needed but not what, and there is no card to open for the
            answer — the session already said it in one sentence via hand_back(reason),
            so show that instead of making the user read the transcript for it. Gated
            on the live status, not the field: a resumed session keeps the old row
            value, and a stale ask beside "Working…" is a claim it is not making. */}
        {/* "Likely complete": the server judged this hand-back a status report on a
            met objective (merged/armed PR, reason that asks nothing). It is a hint
            with two one-tap answers, never an automatic archive — the pill shows
            the bar's reason on hover so the user can check the claim before
            confirming. The buttons stop propagation: the card itself opens on click. */}
        {suspectedComplete && (
          <span
            data-testid="card-suspected-complete"
            className="flex-shrink-0 inline-flex items-center gap-1.5"
          >
            <span
              className="rounded-full border border-success/50 bg-success/10 px-1.5 py-px text-xs font-medium text-success"
              title={`Likely complete: ${suspectedComplete}`}
            >
              Likely complete
            </span>
            {onResolveSuspectedComplete && (
              <>
                <button
                  type="button"
                  data-testid="card-suspected-complete-confirm"
                  onClick={(e) => { e.stopPropagation(); onResolveSuspectedComplete(session.id, 'confirm') }}
                  className="rounded px-1.5 py-px text-xs font-medium text-success hover:bg-success/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  title={`Archive as complete — ${suspectedComplete}`}
                >
                  Mark complete
                </button>
                <button
                  type="button"
                  data-testid="card-suspected-complete-reject"
                  onClick={(e) => { e.stopPropagation(); onResolveSuspectedComplete(session.id, 'reject') }}
                  className="rounded px-1.5 py-px text-xs font-medium text-ink-muted hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  title="Keep it handed back; drop the hint"
                >
                  Not done
                </button>
              </>
            )}
          </span>
        )}
        {handBackReason && (
          <span
            data-testid="card-hand-back-reason"
            className="min-w-0 flex-1 truncate text-ink-muted"
            title={handBackReason}
          >
            {' '}— {handBackReason}
          </span>
        )}
        {/* Suppressed while a hand-back reason shows: two "— …" clauses on one line
            compete, and the specific ask beats the generic auto-summary. */}
        {session.summary && !handBackReason && (
          <span
            data-testid="card-summary"
            className="min-w-0 flex-1 truncate text-ink-muted"
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
            className="ml-auto flex-shrink-0 text-ink-muted tabular-nums"
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
            className={cn(markerBase, 'text-info')}
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
            className={cn(markerBase, 'text-info')}
            title={`Goal (inferred): ${session.effectiveGoal}`}
          >
            <Target size={12} aria-hidden="true" />
            <span className="sr-only">Goal</span>
          </span>
        )}
        {session.autopilotPaused && (
          <span
            data-testid="card-autopilot-badge"
            className={cn(markerBase, 'text-ink-muted')}
            title="Autopilot paused — this session is on manual"
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">Manual</span>
          </span>
        )}
        {showCooldown && (
          <span
            data-testid="card-cooldown-badge"
            className={cn(markerBase, 'text-warning')}
            title={`Autopilot is standing down while you steer this session${cooldownMinsLeft ? ` (${cooldownMinsLeft}m left)` : ''}`}
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">
              Autopilot paused — you're steering{cooldownMinsLeft ? ` (${cooldownMinsLeft}m left)` : ''}
            </span>
          </span>
        )}
        {session.autoArchiveOptOut && (
          /* Indicator: this session will never be auto-archived — it was manually
             unarchived, "Auto-archive" was unticked at creation, or it was disabled
             mid-session from this same menu (the three reasons the server records
             on this column). The menu item above (when provided) is the way back. */
          <span
            data-testid="card-no-autoarchive-badge"
            className={cn(markerBase, 'text-warning')}
            title={
              session.autoArchiveOptOut === 'manual-unarchive'
                ? "Auto-archive off — you unarchived this session, so autopilot won't archive it again"
                : session.autoArchiveOptOut === 'session-toggle'
                  ? 'Auto-archive off — turned off for this session'
                  : 'Auto-archive off — turned off when this session was created'
            }
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
              'rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              // Emerald when autopilot is actively guiding (matches the title
              // marker); generic violet for created-by/other automation, and
              // for an autopilot session that's paused.
              guidedByAutopilot && !session.origin
                ? 'text-success'
                : 'text-info',
            )}
            title={session.origin
              ? `Created by automation: ${session.origin}`
              : `Assisted by automation: ${session.assistedBy}`}
          >
            <Bot size={12} aria-hidden="true" />
            <span className="sr-only">
              {session.origin
                ? `Created by ${session.originLabel ?? session.origin}`
                : `Assisted by ${session.assistedByLabel ?? session.assistedBy}`}
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
            className="hidden @md:inline-flex items-center gap-1 flex-shrink-0 text-xs font-medium px-1.5 py-0.5 rounded bg-danger-wash text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
          const w = bindingWindow(account?.usage, session.model)
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
              'bg-info-wash text-info',
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
              'bg-surface-raised text-ink-muted',
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
                ? 'bg-info-wash text-info'
                : 'bg-surface-raised text-ink-muted',
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
            `absolute z-sticky` popover out of it (same trap CardFrame records for
            the ⋯ menu). */}
        <span
          data-testid="card-account"
          className="min-w-0 max-w-[50%] truncate text-xs text-ink-muted bg-surface-raised px-1.5 py-0.5 rounded"
          title={account?.nickname || session.accountName}
        >
          {account?.nickname || session.accountName}
        </span>
      </div>

      {hasSubAgents && expanded && <SubAgentRows sessionId={session.id} subAgents={subAgents} />}
      {bgSummary && expanded && <BackgroundWorkRows work={backgroundWork} />}
      {hasChildren && expanded && <ChildSessionRows sessions={childSessions} />}
    </CardFrame>
  )
}
