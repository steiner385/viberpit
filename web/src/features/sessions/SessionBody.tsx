/**
 * SessionBody — the main content of a session: the toolbar, the view tabs, the
 * error banner, transcript (with inline rotation dividers), working indicator,
 * action-needed banners, the sr-only aria-live mirror, and the session rail
 * (D-6) — a right column on a pane of 64rem or more, the Details tab below
 * that. Rendered by `MainPaneSession`. Does NOT render the header or composer.
 */

import { useState, useEffect, useMemo, useRef, useId, lazy, Suspense } from 'react'
import { cn } from '@/lib/utils'
import { Tabs } from '@/ui/Tabs'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SessionToolbar } from './SessionToolbar'
import { useRailCollapsed } from './rail/railPreference'
import { railNeedsAttention } from './rail/railAttention'
import { useMcpServers } from './useMcpServers'
import { useContainerMinWidth } from '@/lib/useContainerMinWidth'
import { TranscriptView } from '@/features/sessions/transcript/TranscriptView'
import { SessionPlaceholder } from '@/features/sessions/transcript/SessionPlaceholder'
import { WorkingIndicator } from './WorkingIndicator'
import { CondensingProgress } from '@/features/sessions/transcript/CondensingProgress'
import { PendingQuestionPanel } from '@/features/sessions/panels/PendingQuestionPanel'
import { PendingPermissionPanel } from '@/features/sessions/panels/PendingPermissionPanel'
import { SessionErrorBanner } from '@/features/sessions/panels/SessionErrorBanner'
import { SnoozeBanner } from '@/features/sessions/panels/SnoozeBanner'
import { InterruptedAskNotice } from '@/features/sessions/transcript/InterruptedAskNotice'
import { PendingModelApprovalPanel } from '@/features/sessions/panels/PendingModelApprovalPanel'
import { SessionAriaLive } from './SessionAriaLive'
import { statusTier } from '@/session/statusTier'
import { summaryOutcome } from '@/session/turnSummary'
import { selectRunningSubAgentCount } from '@/session/selectors'
import { DeferredBrowserTab } from '@/features/sessions/browser/DeferredBrowserTab'
import { useSessionDevServers } from '@/api/devServers'
import { useAccounts, useSessions, useModelApproval, useSetAccountPin, useEngineCapabilities, useResolveSuspectedComplete, useSessionDiff, useSetPrAutofix } from '@/api/hooks'
import { canUseEngine } from '@/session/engineCapabilities'
import { SANDBOX_HOLD_REASON } from '@/api/types'
import { useFileTarget, useSessionView, type SessionView } from './sessionView'
import { FileRefContext, type FileRefActions } from '@/features/sessions/transcript/fileRefContext'
import { useComposerAttach } from '@/shell/sessionGestures'
import { ForkProvider } from '@/features/sessions/fork/ForkProvider'
import { RewindProvider } from '@/features/sessions/rewind/RewindProvider'
import { ErrorRecoveryContext, type ErrorRecovery } from '@/features/sessions/transcript/errorRecoveryContext'
import { effortChoices, effortStep } from '@/features/sessions/composer/effortChoices'
import { useAutoMarkViewed } from './useAutoMarkViewed'

/** The Changes pane is off the entry path (D-4): only the header chip ships eagerly. */
const ChangesTab = lazy(() => import('@/features/sessions/diff/ChangesTab'))
/** The Files tab too (D-7): a read-only worktree viewer, off the entry path. */
const FilesTab = lazy(() => import('@/features/sessions/files/FilesTab'))
/** The PR tab too (D-15): checks, review threads and the merge actions. */
const PrTab = lazy(() => import('@/features/sessions/pr/PrTab'))
/** The rail's cards too (D-6): secondary to the transcript, so they load after it. */
const SessionRail = lazy(() => import('./rail/SessionRail').then((m) => ({ default: m.SessionRail })))

/** Local alias: the shared gate now lives beside the hook that feeds it. */
const can = canUseEngine

/** The pane width (rem) from which the rail is a column, not the Details tab. */
const RAIL_MIN_REM = 64

/** `onClosed`: the session ended from inside the body (the rail's worktree Discard,
 *  D-19) — the host detaches and moves on, as after an archive. */
export function SessionBody({ rt, onClosed }: { rt: SessionRuntime; onClosed?: () => void }) {
  const runningSubAgents = selectRunningSubAgentCount(rt.state.turns, rt.state.subAgents)
  const { data: accounts = [] } = useAccounts()
  const { data: sessions = [] } = useSessions()
  const sessionRecord = sessions.find((s) => s.id === rt.sessionId)
  const pinned = sessionRecord?.accountPinned ?? false
  const caps = useEngineCapabilities(sessionRecord?.engine)
  const { mutate: modelApproval, isPending: modelApprovalPending } = useModelApproval()
  const setAccountPin = useSetAccountPin()
  const setPrAutofix = useSetPrAutofix()
  // "Mark complete" / "Not done" on a handed-back session's summary card — the same
  // mutation the list card offers, so the decision is taken beside the evidence.
  const resolveSuspected = useResolveSuspectedComplete()
  // Per-viewer read state (D-10): this is the live open session, so it is read.
  useAutoMarkViewed(rt.sessionId, sessionRecord?.updatedAt)

  // Whether the x11/browser feature is available on this server.
  // Starts as false (unknown); set to true only when /api/x11/metrics returns
  // a metrics object that does NOT have {enabled: false}.
  const [browserEnabled, setBrowserEnabled] = useState(false)
  // Active view, persisted in `?view=` (see `sessionView.ts`).
  const [requestedView, setView] = useSessionView()
  // The Files tab's open file and line (`?file=&line=`), handed to the lazy pane.
  const [fileTarget, setFile] = useFileTarget()

  // The worktree diff summary (D-4): it lists the Changes tab, and the header's
  // chip reads the same query. `busy` going false (a turn ended) refetches it.
  const busy = statusTier(rt.state.status.state) === 'busy'
  // The summary card's hand-off (D-11) — "Asked you" mid-turn, "Handed back" after —
  // for the persistent live region to announce once (`SessionAriaLive`).
  const handOffOutcome = summaryOutcome(rt.state.status.state, sessionRecord?.handBackReason, null, busy)
  const handOff = handOffOutcome?.reason ? { label: handOffOutcome.label, reason: handOffOutcome.reason } : null
  const diffSummary = useSessionDiff(rt.sessionId, { summary: true, busy })
  const hasChanges = (diffSummary.data?.files.length ?? 0) > 0
  // The Files tab (D-7) reads the session's worktree, so it needs one.
  const cwd = sessionRecord?.cwd
  const hasFiles = !!cwd
  // The PR tab (D-15) reads the session payload's PRs, so it needs one.
  const prs = sessionRecord?.prs ?? []
  const hasPrs = prs.length > 0
  // The Browser tab (D-18) is also listed for a session with a detected dev server,
  // even where the edge has no X11, so a viewer at the desk can still open its links.
  // Polled only while the Browser tab is shown; the tab renders the same list.
  const devServers = useSessionDevServers(rt.sessionId, busy, requestedView === 'browser', browserEnabled)
  const hasBrowser = browserEnabled || (devServers.servers?.length ?? 0) > 0
  // Transcript paths under the cwd open the Files tab and attach to the dock
  // composer (D-7, `transcript/PathRef`); without a cwd they stay click-to-copy.
  const attachMention = useComposerAttach()
  const fileRefActions = useMemo<FileRefActions | null>(
    () => (cwd ? { cwd, openFile: setFile, attachMention } : null),
    [cwd, setFile, attachMention],
  )

  // The rail is a column on a pane of 64rem or more — the pane's own width, so
  // the pushed assistant drawer folds it into Details like a narrow window does.
  const [rowRef, wide] = useContainerMinWidth<HTMLDivElement>(RAIL_MIN_REM)
  const [railCollapsed, toggleRail] = useRailCollapsed()
  const railId = useId()
  // The rail's at-a-glance signal while it is out of sight (Details tab, or a
  // collapsed rail): background work running, a failed sub-agent, an MCP
  // server failed or needing auth.
  const mcpServers = useMcpServers(rt.sessionId, rt.state.mcpServers)
  const railAttention = railNeedsAttention({ backgroundWork: rt.backgroundWork, subAgents: rt.subAgents, mcpServers })

  // A view that is not listed falls back to the conversation. Details is listed
  // only where there is no rail column to hold the cards.
  const listed: Record<SessionView, boolean> = {
    conversation: true, changes: hasChanges, pr: hasPrs, files: hasFiles, browser: hasBrowser, details: !wide,
  }
  const view: SessionView = listed[requestedView] ? requestedView : 'conversation'
  const showTabs = hasChanges || hasPrs || hasFiles || hasBrowser || !wide

  // Opening the tab refreshes the summary too (the pane itself fetches the full diff).
  // Keyed on the REQUESTED view changing to Changes after mount — a click on the tab
  // or the header chip. Not on `view`: with `?view=changes` in the URL, `view` only
  // resolves to Changes once the mount fetch has landed (it lists the tab), so that
  // refetched the just-fetched summary and every page load asked for it twice.
  const { refetch: refetchDiffSummary } = diffSummary
  const prevRequestedView = useRef(requestedView)
  useEffect(() => {
    const opened = requestedView === 'changes' && prevRequestedView.current !== 'changes'
    prevRequestedView.current = requestedView
    if (opened) void refetchDiffSummary({ cancelRefetch: false })
  }, [requestedView, refetchDiffSummary])

  useEffect(() => {
    let cancelled = false
    fetch('/api/x11/metrics')
      .then((r) => r.json())
      .then((data) => {
        if (!cancelled && data?.enabled !== false) {
          setBrowserEnabled(true)
        }
      })
      .catch(() => {
        // fetch error → keep browserEnabled=false, no Browser tab
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Held because its sandbox failed (D-23): the error banner offers Retry / Turn off.
  const sandboxHold = sessionRecord?.status === 'needs_attention' && sessionRecord.pausedReason === SANDBOX_HOLD_REASON
    ? { sessionId: sessionRecord.id, reason: sessionRecord.sandbox?.reason ?? null }
    : null

  // An error card's Switch model / Lower effort / Compact (D-19).
  const recovery: ErrorRecovery = {
    rt, busy, session: sessionRecord,
    effortStep: (models) => {
      const { efforts, effort } = effortChoices(rt.model, sessionRecord, caps, models)
      return effortStep(efforts, effort)
    },
  }

  const rail = (
    <Suspense fallback={null}>
      <SessionRail
        changes={{ sessionId: rt.sessionId, busy, onOpenChanges: () => setView('changes') }}
        context={{
          sessionId: rt.sessionId,
          account: rt.account,
          contextPct: rt.contextPct,
          quota: rt.quota,
          commands: rt.commands,
          mcpLive: rt.state.mcpServers,
          mcpToolSwitches: canUseEngine(caps, 'flagSettings'),
          memoryFiles: rt.memoryFiles,
          cwd: sessionRecord?.cwd ?? null,
          onCompact: () => rt.onPlainPrompt('/compact', []),
        }}
        environment={{
          engine: sessionRecord?.engine,
          account: rt.account,
          accounts: accounts.map((a) => ({ name: a.name, nickname: a.nickname })),
          accountPinned: pinned,
          onSelectAccount: (name) => rt.onCommand('account', name),
          onUnpin: () => setAccountPin.mutate({ id: rt.sessionId, pinned: false }),
          host: sessionRecord?.host,
          displayMode: sessionRecord?.displayMode,
          displayFallbackReason: sessionRecord?.displayFallbackReason,
          branch: sessionRecord?.branch,
          cwd: sessionRecord?.cwd,
          // Offered by the card only beside a worktree path (`cwd`).
          worktree: { sessionId: rt.sessionId, busy, onDiscarded: onClosed },
          // Strictly on (D-23): an unknown capability is an older edge with no endpoint.
          sandbox: caps?.sandbox === true
            ? { sessionId: rt.sessionId, busy, state: sessionRecord?.sandbox ?? null }
            : undefined,
          prs: sessionRecord?.prs,
          prAutofix: {
            enabled: sessionRecord?.prAutofixPaused !== true,
            onChange: (enabled) => setPrAutofix.mutate({ id: rt.sessionId, enabled }),
            pending: setPrAutofix.isPending,
          },
        }}
        tasks={{
          sessionId: rt.sessionId,
          subAgentsSupported: can(caps, 'subagents'),
          subAgents: rt.subAgents,
          backgroundWork: rt.backgroundWork,
        }}
      />
    </Suspense>
  )

  return (
    <>
      <SessionToolbar
        sessionId={rt.sessionId}
        status={rt.state.status}
        connectionState={rt.connectionState}
        subAgentCount={runningSubAgents}
        viewMode={rt.viewMode}
        onSelectViewMode={rt.setViewMode}
        rail={wide ? { collapsed: railCollapsed, onToggle: toggleRail, controls: railId, attention: railAttention } : null}
      />

      {/* The row: tabs + the active view, beside the rail column on a wide pane.
          Measured for the rail switch; `min-w-0` keeps the transcript from
          pushing the rail off the pane. */}
      <div ref={rowRef} className="flex min-h-0 min-w-0 flex-1">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {/* Conversation | Changes | PR | Files | Browser | Details — Changes once the
              worktree differs from its base, PR when the session has opened one,
              Files when the session has a cwd, Browser
              when the X display is available or the session has a dev server, Details on a pane too narrow for the
              rail; no strip for the conversation alone. */}
          {showTabs && (
            <Tabs
              label="Session view"
              items={[
                { id: 'conversation', label: 'Conversation' },
                ...(hasChanges ? [{ id: 'changes', label: 'Changes' }] : []),
                ...(hasPrs ? [{ id: 'pr', label: 'PR' }] : []),
                ...(hasFiles ? [{ id: 'files', label: 'Files' }] : []),
                ...(hasBrowser ? [{ id: 'browser', label: 'Browser' }] : []),
                ...(!wide ? [{ id: 'details', label: 'Details', attention: railAttention }] : []),
              ]}
              value={view}
              onChange={(id) => setView(id as SessionView)}
              panelId={(id) => `tab-panel-${id}`}
              className="shrink-0 bg-canvas px-2"
            />
          )}

          {/* Conversation panel content — defined once, rendered in either a plain div
              or a tab-panel wrapper depending on whether the tab strip is shown. */}
          {(() => {
            const conversationPanel = (
              <>
                {/* Error banner — shared with the assistant drawer; see the component
                    for why it is transient and where the durable record lives. */}
                <SessionErrorBanner rt={rt} sandboxHold={sandboxHold} />
                {sessionRecord?.snooze && (
                  <SnoozeBanner sessionId={sessionRecord.id} snooze={sessionRecord.snooze} />
                )}

                {/* Transcript — rotation dividers are interleaved inline by anchor turn.
                    With no turns to show, the transcript area would be blank; the
                    placeholder fills it with an explicit connecting/loading/empty/error
                    state instead so the user always knows what's happening. */}
                {rt.state.turns.length === 0 && rt.state.rotations.length === 0 && !rt.state.rewind ? (
                  <SessionPlaceholder
                    connectionState={rt.connectionState}
                    replaying={rt.state.replaying}
                    hasError={rt.state.error !== null}
                    queued={sessionRecord?.status === 'queued'}
                    queuePosition={sessionRecord?.queuePosition}
                    onRetry={rt.reattach}
                  />
                ) : (
                  <FileRefContext.Provider value={fileRefActions}>
                    {/* "Fork from here" in each turn's menu (D-8), where the engine can fork;
                        "Rewind to here" / "Edit & resend" and the divider's Undo (D-9), where it can rewind. */}
                    <ForkProvider sessionId={rt.sessionId} turns={rt.state.turns}
                                  earliestTurn={rt.state.earliestTurn} enabled={can(caps, 'fork')}>
                     <RewindProvider sessionId={rt.sessionId} enabled={can(caps, 'rewind')}
                                     status={rt.state.status.state} send={rt.onPlainPrompt}
                                     appliedRewindId={rt.state.rewind?.rewindId ?? null}
                                     onExpired={rt.dismissRewind}>
                     <ErrorRecoveryContext.Provider value={recovery}>
                      <TranscriptView
                        sessionId={rt.sessionId}
                        turns={rt.state.turns}
                        rotations={rt.state.rotations}
                        rewind={rt.state.rewind}
                        model={rt.state.model}
                        toolDetailMode={rt.toolDetailMode}
                        thinkingCollapsed={rt.thinkingCollapsed}
                        viewModeEpoch={rt.viewModeEpoch}
                        // Only the live-open block streams as plain text; during replay the open
                        // id is meaningless, so suppress it and render everything as markdown.
                        streamingBlockId={rt.state.replaying ? null : rt.state.openBlockId}
                        onAnswer={rt.onAnswer}
                        subAgents={rt.state.subAgents}
                        earliestTurn={rt.state.earliestTurn}
                        loadingOlder={rt.state.loadingOlder}
                        onLoadOlder={rt.onLoadOlder}
                        followSignal={rt.state.pendingSentSeq}
                        // The post-turn summary card (D-11), the transcript's tail row: how
                        // the turn ended, its files, the replies. The hand-back facts come
                        // from the record (server source of truth for the hand-back row);
                        // `summaryOutcome` gates them on the live status.
                        summary={{
                          status: rt.state.status.state,
                          handBackReason: sessionRecord?.handBackReason,
                          suspectedCompleteReason: sessionRecord?.suspectedCompleteReason,
                          suggestions: rt.suggestions,
                          onPrompt: rt.onPrompt,
                          onResolveSuspectedComplete: (decision) =>
                            resolveSuspected.mutate({ id: rt.sessionId, decision }),
                          resolvePending: resolveSuspected.isPending,
                          onOpenChanges: hasChanges ? () => setView('changes') : undefined,
                        }}
                      />
                     </ErrorRecoveryContext.Provider>
                     </RewindProvider>
                    </ForkProvider>
                  </FileRefContext.Provider>
                )}

                {/* Prominent "waiting for Claude" indicator shown below the last turn */}
                {statusTier(rt.state.status.state) === 'busy' && (
                  rt.state.status.state === 'condensing' ? (
                    <CondensingProgress expectedMs={rt.state.condense?.expectedMs ?? 12000} />
                  ) : (
                    <div
                      aria-hidden="true"
                      className={cn(
                        'flex-shrink-0 flex items-center px-4 py-2',
                        'text-xs text-ink-muted',
                      )}
                    >
                      <WorkingIndicator count={rt.state.status.count} subAgentCount={runningSubAgents} />
                    </div>
                  )
                )}

                {/* Pinned permission panel for the live open permission (respond in place).
                    Mirrors PendingQuestionPanel: the actionable allow/deny PermissionCard
                    otherwise renders only inline in the scrollable transcript, so a pending
                    permission below the fold left the user with a "Permission needed" status
                    and no reachable buttons (just a "scroll up" banner). Pinning it above the
                    composer keeps the controls always visible. */}
                <InterruptedAskNotice
                  interruptedFromStatus={sessionRecord?.interruptedFromStatus}
                  onAskAgain={() => rt.onPrompt(
                    'The permission or plan approval you were waiting on was lost when the '
                    + 'daemon restarted — I never saw it. Please re-request whatever you need '
                    + 'approved, or tell me where things stand.',
                    [], undefined, 'suggested',
                  )}
                />

                <PendingPermissionPanel
                  turns={rt.state.turns}
                  onDecision={rt.onDecision}
                  hasRepo={!!sessionRecord?.repo}
                  onPreviewRule={rt.onPreviewRule}
                  onGrantRule={rt.onGrantRule}
                />

                {/* Pinned model-approval panel for a mid-session gated model switch.
                    `pendingModel` is read from the session record (server source of truth for
                    approval state) — not the reducer, which tracks transcript model events only.
                    Capability-gated on `selfModelSwitch`: the parked hold is produced by the
                    self-directed switch flow and the Claude tier gate, so an engine without it
                    can never own one, and offering the approval buttons would be a lie. */}
                {can(caps, 'selfModelSwitch') && sessionRecord?.pendingModel && (
                  <PendingModelApprovalPanel
                    model={sessionRecord.pendingModel}
                    reason={sessionRecord.pendingModelReason}
                    requestedBy={sessionRecord.pendingModelRequestedBy}
                    onDecision={(decision, model) =>
                      modelApproval({ id: rt.sessionId, decision, model })
                    }
                    pending={modelApprovalPending}
                  />
                )}

                {/* Pinned answer panel for the live open question (answer in place). Lives
                    here in SessionBody so BOTH the mobile SessionView and the desktop
                    master-detail shell render it — the desktop shell previously had no panel,
                    so a live AskUserQuestion (filtered out of the inline transcript by Turn)
                    rendered nowhere and was unanswerable. Hidden for an engine without the
                    `questions` capability — it has no interactive-question channel, so an
                    answer sent from here could only ever come back "no longer pending". */}
                {can(caps, 'questions') && (
                  <PendingQuestionPanel
                    turns={rt.state.turns}
                    onAnswer={rt.onAnswer}
                    onChat={rt.onChat}
                    armedQuestionId={rt.armedQuestionId}
                  />
                )}

                {/* sr-only aria-live mirror (owns its own ref + effect) */}
                <SessionAriaLive turns={rt.state.turns} replaying={rt.state.replaying} handOff={handOff} />
              </>
            )

            /* Changes, PR and Files tab panels — all lazy panes */
            if (view === 'changes' || view === 'pr' || view === 'files') {
              return (
                <div id={`tab-panel-${view}`} role="tabpanel" aria-labelledby={`tab-${view}`} className="flex-1 min-h-0 min-w-0">
                  <Suspense fallback={<div className="h-full w-full" aria-busy="true" />}>
                    {view === 'changes'
                      ? <ChangesTab sessionId={rt.sessionId} onSendReview={rt.onPlainPrompt} />
                      : view === 'pr'
                        ? <PrTab sessionId={rt.sessionId} prs={prs} hasFiles={hasFiles} onOpenFile={setFile}
                                 onShowConversation={() => setView('conversation')} />
                        : <FilesTab sessionId={rt.sessionId} target={fileTarget} onOpen={setFile} />}
                  </Suspense>
                </div>
              )
            }

            /* Details tab panel — the rail's cards, on a pane too narrow for the column */
            if (view === 'details') {
              return (
                <div id="tab-panel-details" role="tabpanel" aria-labelledby="tab-details" className="flex-1 min-h-0 overflow-y-auto">
                  {rail}
                </div>
              )
            }

            /* Browser tab panel */
            if (view === 'browser') {
              return (
                <div id="tab-panel-browser" role="tabpanel" aria-labelledby="tab-browser" className="flex-1 min-h-0">
                  <DeferredBrowserTab sessionId={rt.sessionId} statusState={rt.state.status.state}
                                      displayMode={sessionRecord?.displayMode} host={sessionRecord?.host}
                                      x11Enabled={browserEnabled} devServers={devServers} />
                </div>
              )
            }

            /* Conversation tab panel — with tabpanel ARIA when the tab strip is shown,
               a plain wrapper div when it is not. */
            const convoWrapperProps = showTabs
              ? {
                  role: 'tabpanel' as const,
                  id: 'tab-panel-conversation',
                  'aria-labelledby': 'tab-conversation',
                  className: 'flex-1 min-h-0 flex flex-col',
                }
              : { className: 'flex-1 min-h-0 flex flex-col' }

            return <div {...convoWrapperProps}>{conversationPanel}</div>
          })()}
        </div>

        {wide && !railCollapsed && (
          <aside
            id={railId}
            aria-label="Session details"
            className="w-80 flex-shrink-0 overflow-y-auto border-l border-edge bg-canvas"
          >
            {rail}
          </aside>
        )}
      </div>
    </>
  )
}
