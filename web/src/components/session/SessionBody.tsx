/**
 * SessionBody — the scrollable main content of a session: status strip, error
 * banner, transcript (with inline rotation dividers), working indicator,
 * action-needed banner, and the sr-only aria-live mirror. Rendered by both the
 * mobile SessionView and the desktop master-detail main pane. Does NOT render the
 * top bar or composer.
 */

import { useState, useEffect, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SessionMeta } from './SessionMeta'
import { StatusBar } from './StatusBar'
import { TranscriptView } from './TranscriptView'
import { SessionPlaceholder } from './SessionPlaceholder'
import { WorkingIndicator } from './WorkingIndicator'
import { CondensingProgress } from './CondensingProgress'
import { PendingQuestionPanel } from './PendingQuestionPanel'
import { PendingPermissionPanel } from './PendingPermissionPanel'
import { YourTurnPanel } from './YourTurnPanel'
import { PendingModelApprovalPanel } from './PendingModelApprovalPanel'
import { SessionAriaLive } from './SessionAriaLive'
import { statusTier } from '@/session/statusTier'
import { selectRunningSubAgentCount } from '@/session/selectors'
import { BrowserTab } from '../BrowserTab'
import { useModels, useAccounts, useSessions, useModelApproval } from '@/api/hooks'
import { ExportMenu } from './ExportMenu'
import { ModelSwitchDialog } from './ModelSwitchDialog'

export function SessionBody({ rt, actionsSlot }: { rt: SessionRuntime; actionsSlot?: ReactNode }) {
  const runningSubAgents = selectRunningSubAgentCount(rt.state.turns)
  const { data: models = [] } = useModels()
  const { data: accounts = [] } = useAccounts()
  const { data: sessions = [] } = useSessions()
  const sessionRecord = sessions.find((s) => s.id === rt.sessionId)
  const pinned = sessionRecord?.accountPinned ?? false
  const { mutate: modelApproval } = useModelApproval()

  // Whether the x11/browser feature is available on this server.
  // Starts as false (unknown); set to true only when /api/x11/metrics returns
  // a metrics object that does NOT have {enabled: false}.
  const [browserEnabled, setBrowserEnabled] = useState(false)
  // Active tab: 'conversation' (default) or 'browser'
  const [activeTab, setActiveTab] = useState<'conversation' | 'browser'>('conversation')

  // Model id awaiting confirmation: switching model on a BUSY session either stops
  // the in-flight turn and restarts it on the new model, or waits for the turn to
  // finish — so the picker gates on a choice dialog instead of firing immediately.
  // Non-busy tiers switch directly.
  const [pendingModelId, setPendingModelId] = useState<string | null>(null)

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
      {/* Status strip: account · model · context · quota · live status · sub-agents.
          The live StatusBar is folded into the right of this strip so the session
          pane shows a single status line rather than two stacked bars. */}
      <div className="flex-shrink-0">
        <SessionMeta
          account={rt.account}
          model={rt.model}
          liveModel={rt.liveModel}
          modelPending={rt.modelPending}
          mode={rt.mode}
          contextPct={rt.contextPct}
          quota={rt.quota}
          subAgents={rt.subAgents}
          statusSlot={<StatusBar status={rt.state.status} connectionState={rt.connectionState} subAgentCount={runningSubAgents} />}
          exportSlot={<ExportMenu sessionId={rt.sessionId} />}
          actionsSlot={actionsSlot}
          models={models}
          accounts={accounts.map((a) => ({ name: a.name, nickname: a.nickname }))}
          accountPinned={pinned}
          onSelectModel={(id) => {
            if (statusTier(rt.state.status.state) === 'busy') setPendingModelId(id)
            else rt.onSetModel(id, 'now')
          }}
          onSelectAccount={(name) => rt.onCommand('account', name)}
          onSetMode={rt.onSetMode}
        />
      </div>

      {/* Conversation|Browser tab strip — only rendered when x11 is available */}
      {browserEnabled && (
        <div
          role="tablist"
          aria-label="Session view"
          className={cn(
            'flex-shrink-0 flex border-b border-border',
            'bg-background text-sm',
          )}
        >
          <button
            id="tab-conversation"
            role="tab"
            aria-selected={activeTab === 'conversation'}
            aria-controls="tab-panel-conversation"
            onClick={() => setActiveTab('conversation')}
            className={cn(
              'px-4 py-2 font-medium',
              activeTab === 'conversation'
                ? 'border-b-2 border-primary text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            Conversation
          </button>
          <button
            id="tab-browser"
            role="tab"
            aria-selected={activeTab === 'browser'}
            aria-controls="tab-panel-browser"
            onClick={() => setActiveTab('browser')}
            className={cn(
              'px-4 py-2 font-medium',
              activeTab === 'browser'
                ? 'border-b-2 border-primary text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            Browser
          </button>
        </div>
      )}

      {/* Conversation panel content — defined once, rendered in either a plain div
          or a tab-panel wrapper depending on whether browser tabs are active. */}
      {(() => {
        const conversationPanel = (
          <>
            {/* Error banner */}
            {rt.state.error && (
              <div
                role="alert"
                className={cn(
                  'flex-shrink-0 px-4 py-3 text-sm',
                  'bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-300',
                  'border-b border-red-200 dark:border-red-800',
                )}
              >
                <span className="font-medium">Error: </span>
                {rt.state.error.message}
                {rt.state.error.resetsAt && (
                  <span className="ml-2 text-xs opacity-75">
                    (Resets at {new Date(rt.state.error.resetsAt).toLocaleTimeString()})
                  </span>
                )}
              </div>
            )}

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
                // Only the live-open block streams as plain text; during replay the open
                // id is meaningless, so suppress it and render everything as markdown.
                streamingBlockId={rt.state.replaying ? null : rt.state.openBlockId}
                onAnswer={rt.onAnswer}
                subAgents={rt.state.subAgents}
                earliestTurn={rt.state.earliestTurn}
                loadingOlder={rt.state.loadingOlder}
                onLoadOlder={rt.onLoadOlder}
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
                    'text-xs text-gray-400 dark:text-gray-500',
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
            <PendingPermissionPanel
              turns={rt.state.turns}
              permissionMode={rt.mode}
              onDecision={rt.onDecision}
            />

            {/* Pinned model-approval panel for a mid-session gated model switch.
                `pendingModel` is read from the session record (server source of truth for
                approval state) — not the reducer, which tracks transcript model events only. */}
            {sessionRecord?.pendingModel && (
              <PendingModelApprovalPanel
                model={sessionRecord.pendingModel}
                reason={sessionRecord.pendingModelReason}
                requestedBy={sessionRecord.pendingModelRequestedBy}
                onDecision={(decision, model) =>
                  modelApproval({ id: rt.sessionId, decision, model })
                }
              />
            )}

            {/* Pinned answer panel for the live open question (answer in place). Lives
                here in SessionBody so BOTH the mobile SessionView and the desktop
                master-detail shell render it — the desktop shell previously had no panel,
                so a live AskUserQuestion (filtered out of the inline transcript by Turn)
                rendered nowhere and was unanswerable. */}
            <PendingQuestionPanel
              turns={rt.state.turns}
              onAnswer={rt.onAnswer}
              onChat={rt.onChat}
              armedQuestionId={rt.armedQuestionId}
            />

            {/* Pinned "Your turn" call-to-action for a finished turn. The session
                list flags these sessions loudly, but inside the session the only
                cue was a calm gray pill and an ask buried in transcript prose —
                so pin an explicit CTA with the cached suggestions as one-tap
                replies (see YourTurnPanel). */}
            <YourTurnPanel
              statusState={rt.state.status.state}
              // A detached done session never received a live `suggestions` WS
              // event, so runtime state is empty — fall back to the record cache
              // (the same source the session card's chips render from).
              suggestions={
                rt.state.latestSuggestions.length > 0
                  ? rt.state.latestSuggestions
                  : sessionRecord?.latestSuggestions ?? []
              }
              onPrompt={rt.onPrompt}
            />

            {/* sr-only aria-live mirror (owns its own ref + effect) */}
            <SessionAriaLive turns={rt.state.turns} replaying={rt.state.replaying} />
          </>
        )

        /* Browser tab panel */
        if (browserEnabled && activeTab === 'browser') {
          return (
            <div id="tab-panel-browser" role="tabpanel" aria-labelledby="tab-browser" className="flex-1 min-h-0">
              <BrowserTab sessionId={rt.sessionId} statusState={rt.state.status.state} />
            </div>
          )
        }

        /* Conversation tab panel — with tabpanel ARIA when browser tabs are shown,
           plain wrapper div when browser is not available. */
        const convoWrapperProps = browserEnabled
          ? {
              role: 'tabpanel' as const,
              id: 'tab-panel-conversation',
              'aria-labelledby': 'tab-conversation',
              className: 'flex-1 min-h-0 flex flex-col',
            }
          : { className: 'flex-1 min-h-0 flex flex-col' }

        return <div {...convoWrapperProps}>{conversationPanel}</div>
      })()}

      {/* Choose stop-now vs after-this-turn before switching model on a busy
          session. Cancel just closes: the <select> is controlled by rt.model (the
          record's chosen alias), so it snaps back on its own. */}
      {pendingModelId !== null && (
        <ModelSwitchDialog
          modelLabel={models.find((m) => m.id === pendingModelId)?.label ?? pendingModelId}
          onClose={() => setPendingModelId(null)}
          onConfirm={(when) => {
            rt.onSetModel(pendingModelId, when)
            setPendingModelId(null)
          }}
        />
      )}
    </>
  )
}
