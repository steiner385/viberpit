/**
 * SubAgentReaderPage — live drill-down into one Task sub-agent, as a Reader
 * page (finish-line row 6; page-migration §2.2 as amended: no composer, dock
 * hidden, the page's own actions in the header).
 *
 * Parity with Claude Code's `/tasks` agent view (sub-agent spec 2026-09-11
 * §5.5): `type › description` as the title, status + elapsed · tokens · tools ·
 * last tool, the CLI's progress line while running (its summary once ended), a
 * Stop action, and the sub-agent's OWN permission/question cards pinned and
 * answerable here. There is no composer: the CLI drops user input addressed to
 * a sub-agent (spec §1).
 *
 * Data: `rt.subAgents` (merged live task state) for the header;
 * `rt.state.subAgents[agentId]` for the transcript and the cards. The runtime
 * is the PARENT session's — the route keeps `:id`, so `useOpenSessionId`
 * mounts it as for any open session.
 */
import { useLocation } from 'react-router-dom'
import { Square } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { SubAgentStatus } from '@/session/selectors'
import { STATUS_LABEL, statusTone } from '@/features/sessions/subAgentStatus'
import { agentDuration } from '@/components/session/subAgentDuration'
import { TranscriptView } from '@/components/session/TranscriptView'
import { PendingPermissionPanel } from '@/components/session/PendingPermissionPanel'
import { PendingQuestionPanel } from '@/components/session/PendingQuestionPanel'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb } from '@/ui/Breadcrumb'
import { Button } from '@/ui/Button'

export function SubAgentReaderPage({ sessionId, agentId }: { sessionId: string; agentId: string }) {
  // Back and the breadcrumb return to the parent INSIDE the list scope it is open in.
  const scope = listScopeForPath(useLocation().pathname)
  const parentPath = sessionPath(sessionId, scope)
  const rt = useSessionRuntime(sessionId)
  const thread = rt.state.subAgents[agentId]
  const turns = thread?.turns ?? []
  const meta = rt.subAgents.find((a) => a.toolId === agentId)
  const status: SubAgentStatus = meta?.status ?? 'running'
  const running = status === 'running'
  // Stop can only ever act on a LIVE task (spec §6): a replayed/detached
  // session's Task call with no paired result reads 'running' forever, and
  // taskId is only ever set from a live subAgentStarted/subAgentProgress.
  const canStop = running && meta?.taskId != null
  const now = useNow(1000, running)
  const duration = meta ? agentDuration(meta, now) : null
  const stop = useStopSubAgent()
  const label = meta?.type ?? 'sub-agent'
  const title = meta?.description ? `${label} › ${meta.description}` : label

  const usage = [
    duration,
    meta?.totalTokens != null ? `${meta.totalTokens.toLocaleString()} tokens` : null,
    meta?.toolUses != null ? `${meta.toolUses} ${meta.toolUses === 1 ? 'tool' : 'tools'}` : null,
    running && meta?.lastTool ? `last: ${meta.lastTool}` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="reader">
      <PageHeader
        title={title}
        back={{ to: parentPath, label: 'Back to session' }}
        meta={
          <div className="flex min-w-0 flex-col gap-1">
            <Breadcrumb items={[{ label: rt.title || sessionId.slice(0, 8), to: parentPath }, { label }]} />
            <div className="flex items-center gap-2 text-xs">
              <span aria-hidden className={cn('h-2 w-2 rounded-full', statusTone(status))} />
              <span className="uppercase tracking-wide">{STATUS_LABEL[status]}</span>
              {usage && <span className="tabular-nums">· {usage}</span>}
            </div>
            {running && meta?.activity && <p className="truncate text-xs italic">{meta.activity}</p>}
            {!running && meta?.summary && <p className="line-clamp-3 text-xs text-ink">{meta.summary}</p>}
          </div>
        }
        actions={canStop ? (
          <Button
            variant="danger"
            size="sm"
            iconLeft={<Square size={14} aria-hidden="true" />}
            onClick={() => stop.mutate({ id: sessionId, toolUseId: agentId })}
            disabled={stop.isPending || stop.isSuccess}
            aria-label="Stop sub-agent"
          >
            Stop
          </Button>
        ) : undefined}
      />
      <PageContent scroll={false}>
        {turns.length === 0 ? (
          <EmptyState
            title={running ? 'Sub-agent starting…' : 'No live sub-agent activity'}
            description={running ? undefined : 'Open the parent session while it runs to stream it.'}
          />
        ) : (
          <TranscriptView
            turns={turns}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            onAnswer={rt.onAnswer}
          />
        )}
        {/* The sub-agent's own asks, pinned like the parent view pins them. Cards
            are mirrored into this thread by the reducer (keyed by permId /
            questionId), so answering here resolves the parent's copy too. */}
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
      </PageContent>
    </div>
  )
}
