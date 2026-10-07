/**
 * AssistantSheet — the AI drawer's live content: header, starter chips, transcript
 * and composer, wired to the assistant's own session (never the page's).
 *
 * State machine, in order:
 *   1. `status` loading            → skeleton.
 *   2. assistant not enabled       → a placeholder pointing at Settings.
 *   3. enabled, no session yet     → auto-creates one on mount; a create error shows
 *      a retry button instead of spinning forever.
 *   4. a session id exists         → mount a SECOND SessionRuntimeProvider (its own
 *      WebSocket channel — see `ws/client.ts`'s `SocketChannel` — so attaching the
 *      assistant thread never displaces whatever session the page has open) and
 *      render the real drawer body.
 *
 * The body is a separate child component (`AssistantSheetBody`) rather than inline
 * here so it can call `useSessionRuntimeContext()` — the provider's own descendant,
 * not the component that renders the provider.
 */
import { useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAssistantSession, useAssistantStatus } from '@/api/assistant'
import { useSessions } from '@/api/hooks'
import { SessionRuntimeProvider, useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'
import {
  createSessionFailureMessage,
  describeCreateSessionFailure,
} from '@/features/sessions/createSessionFailure'
import { TranscriptView } from '@/features/sessions/transcript/TranscriptView'
import { SessionComposer } from '@/features/sessions/composer/SessionComposer'
import { SessionErrorBanner } from '@/features/sessions/panels/SessionErrorBanner'
import { PendingPermissionPanel } from '@/features/sessions/panels/PendingPermissionPanel'
import { PendingQuestionPanel } from '@/features/sessions/panels/PendingQuestionPanel'
import { useOpenSessionId } from '@/shell/openSessionId'
import { statusTier } from '@/session/statusTier'
import { Button } from '@/ui/Button'
import { AssistantHeader } from './AssistantHeader'
import { FollowUpChips, StarterChips } from './StarterChips'
import { startersFor } from './starters'
import { useAssistantGestures } from './assistantGestures'

export function AssistantSheet() {
  const status = useAssistantStatus()
  const create = useAssistantSession()

  // Auto-create the assistant's session the first time the drawer is opened while
  // enabled — there is no "start a session" affordance in this UI, unlike the
  // page's sessions, which the user explicitly starts from a prompt.
  useEffect(() => {
    if (status.data?.enabled && !status.data.sessionId && !create.isPending) {
      create.mutate({})
    }
    // Deliberately NOT depending on `create` (mutateAsync/isPending are a fresh
    // identity every render) — only re-fire when the facts that gate the create
    // actually change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.data?.enabled, status.data?.sessionId])

  if (status.isLoading) return <AssistantSkeleton />
  if (!status.data?.enabled) return <AssistantDisabled />

  if (!status.data.sessionId) {
    return create.isError
      ? <AssistantCreateError error={create.error} onRetry={() => { create.mutate({}) }} />
      : <AssistantSkeleton />
  }

  const sessionId = status.data.sessionId
  return (
    <SessionRuntimeProvider key={sessionId} sessionId={sessionId} channel="assistant">
      <AssistantSheetBody />
    </SessionRuntimeProvider>
  )
}

function AssistantSheetBody() {
  const rt = useSessionRuntimeContext()
  const { pathname } = useLocation()
  const openSessionId = useOpenSessionId()
  // A NoticeCard's "Ask about it" prefills the composer rather than sending outright
  // (unlike a starter chip) — a fresh object on every ask, even for the same text
  // twice, so Composer's prefill effect re-fires (see Composer.tsx's externalDraft).
  const [draftSeed, setDraftSeed] = useState<{ text: string } | null>(null)

  // `@` session mentions (spec v2 §10.2): only the assistant drawer's composer gets
  // this — a coding session's Composer never receives mentionSource at all.
  const { data: sessions } = useSessions()
  const mentionSource = useMemo(
    () => (sessions ?? []).map((s) => ({ id: s.id, title: s.name })),
    [sessions],
  )

  // Publish this turn's liveness to `DrawerHost` so Escape can interrupt it before
  // closing the drawer, instead of abandoning an in-flight assistant turn silently.
  useAssistantGestures({
    turnLive: statusTier(rt.state.status.state) === 'busy',
    interrupt: rt.onInterrupt,
  })

  return (
    <div className="flex h-full min-h-0 flex-col">
      <AssistantHeader />
      {/* Session-level errors (a failed wake, an unknown thread) used to reach this
          runtime's reducer and render nowhere — SessionBody was the only surface. */}
      <SessionErrorBanner rt={rt} />
      <div className="min-h-0 flex-1 overflow-y-auto" aria-live="polite">
        <StarterChips starters={startersFor(pathname, openSessionId)} />
        <TranscriptView
          variant="assistant"
          turns={rt.state.turns}
          rotations={rt.state.rotations}
          model={rt.state.model}
          toolDetailMode={rt.toolDetailMode}
          thinkingCollapsed={rt.thinkingCollapsed}
          // Only the live-open block streams as plain text; during replay the open
          // id is meaningless (mirrors SessionBody's TranscriptView call).
          streamingBlockId={rt.state.replaying ? null : rt.state.openBlockId}
          onAnswer={rt.onAnswer}
          subAgents={rt.state.subAgents}
          earliestTurn={rt.state.earliestTurn}
          loadingOlder={rt.state.loadingOlder}
          onLoadOlder={rt.onLoadOlder}
          onAsk={(text) => setDraftSeed({ text })}
        />
        <FollowUpChips />
      </div>
      {/* Pinned permission / question panels, exactly as SessionBody mounts them. Turn
          filters open permissions and live questions OUT of the transcript on the
          assumption that these panels render them above the composer — an assumption
          the drawer did not honour until 2026-09-16, when the assistant's gated
          `dispatch_backlog` cards (DESTRUCTIVE → always a card, even in full-auto)
          were pended and re-emitted by the daemon but had no surface here, so the
          operator saw nothing and their next message steer-denied each one. The
          gate is unchanged; this only gives it somewhere to be answered. */}
      <PendingPermissionPanel
        turns={rt.state.turns}
        permissionMode={rt.mode}
        onDecision={rt.onDecision}
      />
      <PendingQuestionPanel
        turns={rt.state.turns}
        onAnswer={rt.onAnswer}
        onChat={rt.onChat}
        armedQuestionId={rt.armedQuestionId}
      />
      <SessionComposer
        rt={rt}
        hideModeSelector
        contextProvider={() => ({ route: pathname, ...(openSessionId ? { sessionId: openSessionId } : {}) })}
        externalDraft={draftSeed}
        mentionSource={mentionSource}
      />
      <p className="border-t border-edge px-3 py-1 text-xs text-ink-muted">
        AI-generated. Actions ask for confirmation.
      </p>
    </div>
  )
}

function AssistantSkeleton() {
  return (
    <div className="flex h-full flex-col gap-3 p-4" aria-busy="true">
      <div className="h-4 w-1/3 animate-pulse rounded bg-surface-raised" />
      <div className="h-4 w-2/3 animate-pulse rounded bg-surface-raised" />
      <div className="h-4 w-1/2 animate-pulse rounded bg-surface-raised" />
    </div>
  )
}

function AssistantDisabled() {
  return (
    <div className="space-y-3 p-4 text-sm text-ink-muted">
      <p className="font-medium text-ink">The assistant isn&rsquo;t enabled.</p>
      <p>Enable it in Settings to start using the assistant.</p>
    </div>
  )
}

/**
 * Read through the shared create-session triage rather than `Error.message`: an
 * `ApiRequestError`'s message is the literal `API error <status>`, so a fleet
 * refusal showed the user "API error 500" and nothing else (#2184). The drawer
 * creates an ordinary session, so it gets the New Session dialog's reading of
 * the same failures for free.
 */
function AssistantCreateError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const message = createSessionFailureMessage(describeCreateSessionFailure(error))
  return (
    <div className="space-y-3 p-4 text-sm">
      <p className="font-medium text-ink">Couldn&rsquo;t start the assistant</p>
      <p className="text-ink-muted">{message}</p>
      <Button type="button" variant="secondary" onClick={onRetry}>Retry</Button>
    </div>
  )
}
