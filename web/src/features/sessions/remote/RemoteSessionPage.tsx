/**
 * RemoteSessionPage — `/sessions/remote/:remoteId`: a Claude Code Remote Control session
 * ViberPit does not run, watched live and driven from here.
 *
 * Built from the session page's own parts, not a twin of them: the transcript is
 * `TranscriptView` over the shared reducer (fed by `useRemoteSessionRuntime`, which is
 * `useSessionRuntime` attached to `remote:<id>`), the pinned cards are
 * `PendingPermissionPanel` / `PendingQuestionPanel`, the input is `Composer`. Only the
 * handlers differ — prompts and answers go over REST (see the runtime hook).
 *
 * What is deliberately absent: everything that acts on a ViberPit session record —
 * account / rotation, model and effort pickers, mode, archive, rewind / fork, the rail
 * (worktree, PR, sandbox, Remote Control switch), the Changes/PR/Files tabs, Stop (no
 * interrupt endpoint), attachments (the prompt endpoint takes text). The session lives
 * elsewhere; a banner says where, and links out to the Claude app.
 *
 * Lazy (`shell/routes.tsx`): none of this adds to the initial chunk. The route is a
 * Reader route for the shell (`readerRouteForPath`), so the dock hides and this page
 * carries its own composer.
 */
import { useMemo } from 'react'
import { ExternalLink } from 'lucide-react'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { statusTier } from '@/session/statusTier'
import { deriveHistory } from '@/session/history'
import { selectRunningSubAgentCount } from '@/session/selectors'
import { SESSIONS_PATH } from '@/session/sessionNavState'
import { useRemoteSessions, remoteWsId } from '@/api/remoteSessionsHooks'
import { PageHeader } from '@/ui/PageHeader'
import { Banner } from '@/ui/Banner'
import { TranscriptView } from '@/features/sessions/transcript/TranscriptView'
import { SessionPlaceholder } from '@/features/sessions/transcript/SessionPlaceholder'
import { PendingPermissionPanel } from '@/features/sessions/panels/PendingPermissionPanel'
import { PendingQuestionPanel } from '@/features/sessions/panels/PendingQuestionPanel'
import { SessionErrorBanner } from '@/features/sessions/panels/SessionErrorBanner'
import { WorkingIndicator } from '@/features/sessions/WorkingIndicator'
import { Composer } from '@/features/sessions/composer/Composer'
import { useRemoteSessionRuntime } from './useRemoteSessionRuntime'
import { remoteMachineLabel, remoteOriginLabel, remoteStatus } from './remoteSessionCopy'

export function RemoteSessionPage({ remoteId }: { remoteId: string }) {
  const remote = useRemoteSessionRuntime(remoteId)
  const { base: rt } = remote
  const record = useRemoteSessions().data?.sessions.find((s) => s.id === remoteId)
  const title = record?.title || rt.title || remoteId
  const busy = statusTier(rt.state.status.state) === 'busy'
  const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
  const runningSubAgents = selectRunningSubAgentCount(rt.state.turns, rt.state.subAgents)
  const url = isSafeHttpUrl(record?.url) ? record.url : null
  const meta = record
    ? [remoteStatus(record).label, remoteOriginLabel(record.origin), record.account, record.cwd]
        .filter(Boolean).join(' · ')
    : 'Remote Control session'

  const openInApp = url && (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex min-h-target items-center gap-1 text-xs text-accent hover:underline"
    >
      Open in Claude app <ExternalLink size={12} aria-hidden="true" />
    </a>
  )

  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="workspace" data-testid="remote-session-page">
      <PageHeader title={title} back={{ to: SESSIONS_PATH, label: 'Back to sessions' }} meta={meta} />

      <Banner
        tone="info"
        role="none"
        className="mx-3 my-2 flex-shrink-0"
        title={`Running on ${remoteMachineLabel(record?.machineId ?? null)} — controlled via Remote Control`}
        actions={openInApp}
      />

      {remote.gone && (
        <Banner tone="warning" role="alert" className="mx-3 mb-2 flex-shrink-0" title="This session is gone">
          claude.ai no longer has this session, so it can't take prompts or answers any more.
        </Banner>
      )}
      {remote.notice && (
        <Banner tone="danger" className="mx-3 mb-2 flex-shrink-0" onDismiss={remote.dismissNotice}>
          {remote.notice}
        </Banner>
      )}

      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-surface">
        {!remote.gone && <SessionErrorBanner rt={rt} />}
        {rt.state.turns.length === 0 ? (
          <SessionPlaceholder
            connectionState={rt.connectionState}
            replaying={rt.state.replaying}
            hasError={rt.state.error !== null || remote.gone}
            onRetry={rt.reattach}
          />
        ) : (
          <TranscriptView
            turns={rt.state.turns}
            model={rt.state.model}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            viewModeEpoch={rt.viewModeEpoch}
            streamingBlockId={rt.state.replaying ? null : rt.state.openBlockId}
            onAnswer={remote.onAnswer}
            subAgents={rt.state.subAgents}
          />
        )}

        {busy && (
          <div aria-hidden="true" className="flex-shrink-0 flex items-center px-4 py-2 text-xs text-ink-muted">
            <WorkingIndicator count={rt.state.status.count} subAgentCount={runningSubAgents} />
          </div>
        )}

        {!remote.gone && (
          <>
            {/* No rule wiring: a remote session has no standing grants, so the card
                offers Allow / Deny only (the server sends `alwaysAllowed: false`). */}
            <PendingPermissionPanel turns={rt.state.turns} onDecision={remote.onDecision} />
            <PendingQuestionPanel
              turns={rt.state.turns}
              onAnswer={remote.onAnswer}
              onChat={rt.onChat}
              armedQuestionId={rt.armedQuestionId}
            />
          </>
        )}
      </div>

      {!remote.gone && (
        <Composer
          status={rt.state.status}
          onPrompt={remote.onPrompt}
          chatArmed={rt.chatArmed}
          autoFocusKey={remoteId}
          history={history}
          // The draft key: never a ViberPit session id, so no session lookup matches it.
          sessionId={remoteWsId(remoteId)}
          returnedDraft={remote.returnedDraft}
          allowAttachments={false}
        />
      )}
    </div>
  )
}

