/**
 * SessionBody — the scrollable main content of a session: status strip, error
 * banner, transcript (with inline rotation dividers), working indicator,
 * action-needed banner, and the sr-only aria-live mirror. Rendered by both the
 * mobile SessionView and the desktop master-detail main pane. Does NOT render the
 * top bar or composer.
 */

import { cn } from '@/lib/utils'
import { statusTier } from '@/session/statusTier'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SessionMeta } from './SessionMeta'
import { StatusBar } from './StatusBar'
import { TranscriptView } from './TranscriptView'
import { SessionPlaceholder } from './SessionPlaceholder'
import { WorkingIndicator } from './WorkingIndicator'
import { PendingQuestionPanel } from './PendingQuestionPanel'
import { SessionAriaLive } from './SessionAriaLive'

export function SessionBody({ rt }: { rt: SessionRuntime }) {
  return (
    <>
      {/* Status strip: account · model · context · quota · live status · sub-agents.
          The live StatusBar is folded into the right of this strip so the session
          pane shows a single status line rather than two stacked bars. */}
      <div className="flex-shrink-0">
        <SessionMeta
          account={rt.account}
          model={rt.state.model}
          contextPct={rt.contextPct}
          quota={rt.quota}
          subAgents={rt.subAgents}
          statusSlot={<StatusBar status={rt.state.status} connectionState={rt.connectionState} />}
        />
      </div>

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
        />
      ) : (
        <TranscriptView
          turns={rt.state.turns}
          rotations={rt.state.rotations}
          permissionMode={rt.mode}
          toolDetailMode={rt.toolDetailMode}
          // Only the live-open block streams as plain text; during replay the open
          // id is meaningless, so suppress it and render everything as markdown.
          streamingBlockId={rt.state.replaying ? null : rt.state.openBlockId}
          onDecision={rt.onDecision}
          onAnswer={rt.onAnswer}
          subAgents={rt.state.subAgents}
        />
      )}

      {/* Prominent "waiting for Claude" indicator shown below the last turn */}
      {statusTier(rt.state.status.state) === 'busy' && (
        <div
          aria-hidden="true"
          className={cn(
            'flex-shrink-0 flex items-center px-4 py-2',
            'text-xs text-gray-400 dark:text-gray-500',
          )}
        >
          <WorkingIndicator count={rt.state.status.count} />
        </div>
      )}

      {/* "Action needed" sticky banner when an open permission is pending.
          Live questions are pinned in PendingQuestionPanel (Task 4) — they no
          longer need the banner for discoverability. */}
      {rt.hasOpenPermission && (
        <div
          aria-live="polite"
          className={cn(
            'flex-shrink-0 px-4 py-2 text-xs font-medium',
            'bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300',
            'border-t border-amber-200 dark:border-amber-800',
          )}
        >
          Action needed — scroll up to respond
        </div>
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

      {/* sr-only aria-live mirror (owns its own ref + effect) */}
      <SessionAriaLive turns={rt.state.turns} replaying={rt.state.replaying} />
    </>
  )
}
