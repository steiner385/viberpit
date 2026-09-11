/**
 * SubSessionView — live drill-down into a single Task sub-agent.
 *
 * Parity with Claude Code's own `/tasks` agent view (spec 2026-09-11 §5.5): a header
 * of `type › description`, status, elapsed · tokens · tools · last tool, the CLI's
 * progress line while running (its summary once ended), a Stop action, and the
 * sub-agent's OWN permission/question cards pinned and answerable here. There is no
 * composer: the CLI drops any user input addressed to a sub-agent (spec §1).
 *
 * Data: `rt.subAgents` (selectSubAgents, merged with the thread's live task state) for
 * the header; `rt.state.subAgents[agentId]` for the transcript and the cards.
 */
import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { SubAgentStatus } from '@/session/selectors'
import { agentDuration } from './subAgentDuration'
import { TranscriptView } from './TranscriptView'
import { PendingPermissionPanel } from './PendingPermissionPanel'
import { PendingQuestionPanel } from './PendingQuestionPanel'

interface Props {
  sessionId: string
  agentId: string
  onBack: () => void
}

const STATUS_DOT: Record<SubAgentStatus, string> = {
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-red-500',
  stopped: 'bg-gray-400',
}
const STATUS_LABEL: Record<SubAgentStatus, string> = {
  running: 'running', done: 'completed', failed: 'failed', stopped: 'stopped',
}

export function SubSessionView({ sessionId, agentId, onBack }: Props) {
  const navigate = useNavigate()
  // The breadcrumb returns to the parent INSIDE the list scope it is open in.
  const scope = listScopeForPath(useLocation().pathname)
  const rt = useSessionRuntime(sessionId)
  const thread = rt.state.subAgents[agentId]
  const turns = thread?.turns ?? []
  const meta = rt.subAgents.find((a) => a.toolId === agentId)
  const status: SubAgentStatus = meta?.status ?? 'running'
  const running = status === 'running'
  // Stop can only ever act on a LIVE task (spec §6): a replayed/detached session's
  // Task call with no paired result reads 'running' forever, and taskId is only ever
  // set from a live subAgentStarted/subAgentProgress. Gate on that, not on
  // `rt.connectionState` — a detached session attaches fine over a connected socket.
  const canStop = running && meta?.taskId != null
  const now = useNow(1000, running)
  const duration = meta ? agentDuration(meta, now) : null
  const stop = useStopSubAgent()
  const label = meta?.type ?? 'sub-agent'

  const usage = [
    duration,
    meta?.totalTokens != null ? `${meta.totalTokens.toLocaleString()} tokens` : null,
    meta?.toolUses != null ? `${meta.toolUses} ${meta.toolUses === 1 ? 'tool' : 'tools'}` : null,
    running && meta?.lastTool ? `last: ${meta.lastTool}` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col flex-1 min-h-0 bg-gray-50 dark:bg-gray-900">
      {/* paddingTop pushes content below the iOS status bar; env() is 0 on desktop. */}
      <header
        className={cn('flex flex-col gap-1 px-3 pb-2 border-b',
          'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800')}
        style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.75rem)' }}
      >
        <div className="flex items-center gap-3">
          <button type="button" onClick={onBack} aria-label="Back to session"
            className={cn('rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
              'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300',
              'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
            ← Back
          </button>
          <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300 truncate">
            <button
              type="button"
              onClick={() => navigate(sessionPath(sessionId, scope))}
              className="opacity-80 hover:underline focus-visible:outline-none focus-visible:underline"
            >
              {rt.title || sessionId.slice(0, 8)}
            </button>
            <span aria-hidden className="mx-1.5 opacity-50">›</span>
            <span className="font-medium">{label}</span>
          </nav>
          {canStop && (
            <button
              type="button"
              onClick={() => stop.mutate({ id: sessionId, toolUseId: agentId })}
              disabled={stop.isPending || stop.isSuccess}
              aria-label="Stop sub-agent"
              className={cn('ml-auto rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
                'border border-red-300 dark:border-red-700 text-red-700 dark:text-red-300',
                'hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500')}
            >
              Stop
            </button>
          )}
        </div>
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 truncate">
          {label}{meta?.description ? ` › ${meta.description}` : ''}
        </h2>
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <span aria-hidden className={cn('h-2 w-2 rounded-full', STATUS_DOT[status])} />
          <span className="uppercase tracking-wide">{STATUS_LABEL[status]}</span>
          {usage && <span className="tabular-nums">· {usage}</span>}
        </div>
        {running && meta?.activity && (
          <p className="text-xs italic text-gray-500 dark:text-gray-400 truncate">{meta.activity}</p>
        )}
        {!running && meta?.summary && (
          <p className="text-xs text-gray-600 dark:text-gray-300 line-clamp-3">{meta.summary}</p>
        )}
      </header>
      {/* paddingBottom keeps last item above the home indicator; env() is 0 on desktop. */}
      {/* A `div`, not a `<main>`: the shell's `app.main` region IS the page's single
          main landmark, and a second one nested inside it is a landmark violation. */}
      <div className="flex-1 overflow-hidden flex flex-col" style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}>
        {turns.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {running
                ? 'Sub-agent starting…'
                : 'No live sub-agent activity (open the parent session while it runs to stream it).'}
            </p>
          </div>
        ) : (
          <TranscriptView
            turns={turns}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            onAnswer={rt.onAnswer}
          />
        )}
        {/* The sub-agent's own asks, pinned like the parent view pins them. Cards are
            mirrored into this thread by the reducer (keyed by permId/questionId), so
            answering here resolves the parent's copy too. */}
        <PendingQuestionPanel
          turns={turns}
          onAnswer={rt.onAnswer}
          onChat={rt.onChat}
          armedQuestionId={rt.armedQuestionId}
        />
        <PendingPermissionPanel
          turns={turns}
          permissionMode={rt.mode}
          onDecision={rt.onDecision}
        />
      </div>
    </div>
  )
}
