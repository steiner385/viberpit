/**
 * SessionBody — the scrollable main content of a session: status strip, error
 * banner, transcript (with inline rotation dividers), working indicator,
 * action-needed banner, and the sr-only aria-live mirror. Rendered by both the
 * mobile SessionView and the desktop master-detail main pane. Does NOT render the
 * top bar or composer.
 */

import { useState, useEffect, useRef, lazy, Suspense, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Tabs } from '@/ui/Tabs'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SessionMeta } from './SessionMeta'
import { StatusBar } from './StatusBar'
import { SessionRulesReadout } from './SessionRulesReadout'
import { DeferredMcpStatusReadout } from './DeferredMcpStatusReadout'
import { TranscriptView } from '@/features/sessions/transcript/TranscriptView'
import { ViewModeMenu } from '@/features/sessions/transcript/ViewModeMenu'
import { SessionPlaceholder } from '@/features/sessions/transcript/SessionPlaceholder'
import { WorkingIndicator } from './WorkingIndicator'
import { CondensingProgress } from '@/features/sessions/transcript/CondensingProgress'
import { PendingQuestionPanel } from '@/features/sessions/panels/PendingQuestionPanel'
import { PendingPermissionPanel } from '@/features/sessions/panels/PendingPermissionPanel'
import { SessionErrorBanner } from '@/features/sessions/panels/SessionErrorBanner'
import { InterruptedAskNotice } from '@/features/sessions/transcript/InterruptedAskNotice'
import { YourTurnPanel } from '@/features/sessions/panels/YourTurnPanel'
import { PendingModelApprovalPanel } from '@/features/sessions/panels/PendingModelApprovalPanel'
import { SessionAriaLive } from './SessionAriaLive'
import { statusTier } from '@/session/statusTier'
import { selectRunningSubAgentCount } from '@/session/selectors'
import { DeferredBrowserTab } from '@/features/sessions/browser/DeferredBrowserTab'
import { useAccounts, useSessions, useModelApproval, useSetAccountPin, useEngineCapabilities, useResolveSuspectedComplete, useSessionDiff } from '@/api/hooks'
import { canUseEngine } from '@/session/engineCapabilities'
import { ExportMenu } from './ExportMenu'
import { useSessionView, type SessionView } from './sessionView'

/** The Changes pane is off the entry path (D-4): only the header chip ships eagerly. */
const ChangesTab = lazy(() => import('@/features/sessions/diff/ChangesTab'))

/** Local alias: the shared gate now lives beside the hook that feeds it. */
const can = canUseEngine

export function SessionBody({ rt, actionsSlot }: { rt: SessionRuntime; actionsSlot?: ReactNode }) {
  const runningSubAgents = selectRunningSubAgentCount(rt.state.turns, rt.state.subAgents)
  const { data: accounts = [] } = useAccounts()
  const { data: sessions = [] } = useSessions()
  const sessionRecord = sessions.find((s) => s.id === rt.sessionId)
  const pinned = sessionRecord?.accountPinned ?? false
  const caps = useEngineCapabilities(sessionRecord?.engine)
  const { mutate: modelApproval, isPending: modelApprovalPending } = useModelApproval()
  const setAccountPin = useSetAccountPin()
  // "Mark complete" / "Not done" on a handed-back session — the same mutation the
  // list card offers, so the decision can be taken beside the transcript evidence.
  const resolveSuspected = useResolveSuspectedComplete()

  // Whether the x11/browser feature is available on this server.
  // Starts as false (unknown); set to true only when /api/x11/metrics returns
  // a metrics object that does NOT have {enabled: false}.
  const [browserEnabled, setBrowserEnabled] = useState(false)
  // Active view, persisted in `?view=` (see `sessionView.ts`).
  const [requestedView, setView] = useSessionView()

  // The worktree diff summary (D-4): it lists the Changes tab, and the header's
  // chip reads the same query. `busy` going false (a turn ended) refetches it.
  const busy = statusTier(rt.state.status.state) === 'busy'
  const diffSummary = useSessionDiff(rt.sessionId, { summary: true, busy })
  const hasChanges = (diffSummary.data?.files.length ?? 0) > 0

  // A view that is not listed falls back to the conversation.
  const listed: Record<SessionView, boolean> = { conversation: true, changes: hasChanges, browser: browserEnabled }
  const view: SessionView = listed[requestedView] ? requestedView : 'conversation'
  const showTabs = hasChanges || browserEnabled

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

  return (
    <>
      {/* Status strip: account · context · quota · live status · sub-agents (the model
          and effort pickers are in the composer's prompt row).
          The live StatusBar is folded into the right of this strip so the session
          pane shows a single status line rather than two stacked bars. */}
      <div className="flex-shrink-0">
        <SessionMeta
          sessionId={rt.sessionId}
          account={rt.account}
          engine={sessionRecord?.engine}
          host={sessionRecord?.host}
          displayMode={sessionRecord?.displayMode}
          displayFallbackReason={sessionRecord?.displayFallbackReason}
          subAgentsSupported={can(caps, 'subagents')}
          quota={rt.quota}
          subAgents={rt.subAgents}
          backgroundWork={rt.backgroundWork}
          statusSlot={<StatusBar status={rt.state.status} connectionState={rt.connectionState} subAgentCount={runningSubAgents} />}
          mcpSlot={<DeferredMcpStatusReadout sessionId={rt.sessionId} account={rt.account} live={rt.state.mcpServers} />}
          rulesSlot={<SessionRulesReadout sessionId={rt.sessionId} />}
          viewModeSlot={<ViewModeMenu mode={rt.viewMode} onSelect={rt.setViewMode} />}
          exportSlot={<ExportMenu sessionId={rt.sessionId} />}
          actionsSlot={actionsSlot}
          accounts={accounts.map((a) => ({ name: a.name, nickname: a.nickname }))}
          accountPinned={pinned}
          onSelectAccount={(name) => rt.onCommand('account', name)}
          onUnpin={() => setAccountPin.mutate({ id: rt.sessionId, pinned: false })}
        />
      </div>

      {/* Conversation | Changes | Browser — Changes once the worktree differs from
          its base, Browser when the X display is available; no strip for the
          conversation alone. */}
      {showTabs && (
        <Tabs
          label="Session view"
          items={[
            { id: 'conversation', label: 'Conversation' },
            ...(hasChanges ? [{ id: 'changes', label: 'Changes' }] : []),
            ...(browserEnabled ? [{ id: 'browser', label: 'Browser' }] : []),
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
            <SessionErrorBanner rt={rt} />

            {/* Transcript — rotation dividers are interleaved inline by anchor turn.
                With no turns to show, the transcript area would be blank; the
                placeholder fills it with an explicit connecting/loading/empty/error
                state instead so the user always knows what's happening. */}
            {rt.state.turns.length === 0 && rt.state.rotations.length === 0 ? (
              <SessionPlaceholder
                connectionState={rt.connectionState}
                replaying={rt.state.replaying}
                hasError={rt.state.error !== null}
                queued={sessionRecord?.status === 'queued'}
                queuePosition={sessionRecord?.queuePosition}
                onRetry={rt.reattach}
              />
            ) : (
              <TranscriptView
                turns={rt.state.turns}
                rotations={rt.state.rotations}
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
              />
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

            {/* Pinned call-to-action for a finished turn ('done') or a hand-back
                ('handed_back'). The session list flags these sessions loudly, but
                inside the session the only cue was a calm gray pill (or an amber
                one) and an ask buried in transcript prose — so pin an explicit CTA
                with the cached suggestions as one-tap replies, and for a hand-back
                lead with the session's own reason (see YourTurnPanel). */}
            <YourTurnPanel
              statusState={rt.state.status.state}
              suggestions={rt.suggestions}
              onPrompt={rt.onPrompt}
              // Both read from the record (server source of truth for the hand-back
              // row); the panel itself gates them on the live status.
              handBackReason={sessionRecord?.handBackReason}
              suspectedCompleteReason={sessionRecord?.suspectedCompleteReason}
              onResolveSuspectedComplete={(decision) =>
                resolveSuspected.mutate({ id: rt.sessionId, decision })
              }
              resolvePending={resolveSuspected.isPending}
            />

            {/* sr-only aria-live mirror (owns its own ref + effect) */}
            <SessionAriaLive turns={rt.state.turns} replaying={rt.state.replaying} />
          </>
        )

        /* Changes tab panel */
        if (view === 'changes') {
          return (
            <div id="tab-panel-changes" role="tabpanel" aria-labelledby="tab-changes" className="flex-1 min-h-0 min-w-0">
              <Suspense fallback={<div className="h-full w-full" aria-busy="true" />}>
                <ChangesTab sessionId={rt.sessionId} onSendReview={rt.onPrompt} />
              </Suspense>
            </div>
          )
        }

        /* Browser tab panel */
        if (view === 'browser') {
          return (
            <div id="tab-panel-browser" role="tabpanel" aria-labelledby="tab-browser" className="flex-1 min-h-0">
              <DeferredBrowserTab sessionId={rt.sessionId} statusState={rt.state.status.state}
                                  displayMode={sessionRecord?.displayMode} host={sessionRecord?.host} />
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
    </>
  )
}
