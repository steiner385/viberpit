/**
 * SessionComposer — single source of truth for wiring a live SessionRuntime into
 * the Composer. Both layouts mount it: the mobile/full-screen SessionView and the
 * desktop master-detail DesktopShell. Assembling the Composer prop bag in one place
 * stops the two call sites from drifting (a dropped `suggestions` prop on desktop is
 * exactly the bug this consolidates away).
 */

import { useMemo, useCallback, type Ref } from 'react'
import { Composer, type ComposerAttach } from './Composer'
import type { MentionSource } from './MentionMenu'
import { PromptRow, type PromptRowMenus } from './PromptRow'
import { useMentionSessions } from './useMentionSessions'
import { deriveHistory } from '@/session/history'
import { useEngineCapabilities, useGenerateSuggestions, useModels, useSessions, useSetFastMode } from '@/api/hooks'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { PromptContext } from '@/session/events'

interface Props {
  rt: SessionRuntime
  /** When set, every prompt sent through this composer — queued or Send now —
   *  carries the returned context (the AI assistant drawer's "sent from" chip and
   *  side-chat scope). Computed lazily at send time (not once) so it always
   *  reflects the current route/open session. */
  contextProvider?: () => PromptContext
  /** Hide the prompt row (mode / model / effort pickers) — the AI assistant
   *  drawer has no session controls, so its lookups are skipped too. */
  hideSessionControls?: boolean
  /** Imperative openers for the prompt row's menus (keyboard shortcuts). */
  promptRowMenus?: Ref<PromptRowMenus>
  /** Prefill the draft from outside the composer (e.g. a NoticeCard's "Ask about it"). */
  externalDraft?: { text: string } | null
  /** `@` session mentions for the AI assistant drawer (spec v2 §10.2), which stays
   *  sessions-only. A session composer builds its own Files + Sessions menu instead. */
  mentionSource?: MentionSource[]
  /** The composer's attach handle (D-7: a transcript path's "Attach as @"). */
  attachRef?: Ref<ComposerAttach>
}

export function SessionComposer({ rt, contextProvider, hideSessionControls = false, promptRowMenus, externalDraft, mentionSource, attachRef }: Props) {
  const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
  // D-7: a session composer `@`-mentions the other sessions (Sessions group) and, with
  // a cwd, its worktree files (Files group, first). An engine without a cwd gets the
  // sessions-only menu and never queries files. The assistant drawer (no session
  // controls) keeps its own sessions-only list.
  const { data: sessions } = useSessions()
  const hasCwd = !hideSessionControls && !!sessions?.find((s) => s.id === rt.sessionId)?.cwd
  const otherSessions = useMentionSessions(rt.sessionId)
  const genSuggestions = useGenerateSuggestions()
  const onPrompt = useCallback(
    (text: string, attachmentIds: string[], context?: PromptContext, replySource?: 'suggested') => {
      if (replySource) { rt.onPrompt(text, attachmentIds, contextProvider?.() ?? context, replySource); return }
      if (contextProvider) { rt.onPrompt(text, attachmentIds, contextProvider()); return }
      rt.onPrompt(text, attachmentIds)
    },
    [rt, contextProvider],
  )
  // Send now carries the same context as a queued send: in the Assistant drawer the
  // context is the side chat's scope, so dropping it here would unscope the turn (D-12).
  const scopedInterruptAndPrompt = useCallback(
    (text: string, attachmentIds: string[], replySource?: 'suggested') =>
      rt.onInterruptAndPrompt(text, attachmentIds, replySource, contextProvider?.()),
    [rt, contextProvider],
  )
  const onInterruptAndPrompt = contextProvider ? scopedInterruptAndPrompt : rt.onInterruptAndPrompt
  return (
    <>
      <Composer
        status={rt.state.status}
        onPrompt={onPrompt}
        onActivity={rt.onActivity}
        onInterrupt={rt.onInterrupt}
        onInterruptAndPrompt={onInterruptAndPrompt}
        queued={rt.state.queued}
        onCancelQueued={rt.onCancelQueued}
        chatArmed={rt.chatArmed}
        commands={rt.commands}
        onCommand={rt.onCommand}
        autoFocusKey={rt.sessionId}
        suggestions={rt.suggestions}
        onRequestSuggestions={() => genSuggestions.mutateAsync({ id: rt.sessionId })}
        history={history}
        sessionId={rt.sessionId}
        externalDraft={externalDraft}
        mentionSource={hideSessionControls ? mentionSource : otherSessions}
        fileMentionSessionId={hasCwd ? rt.sessionId : undefined}
        // The composer with session controls IS the session's; the assistant drawer hides them.
        sessionComposer={!hideSessionControls}
        attachRef={attachRef}
      />
      {!hideSessionControls && <SessionPromptRow rt={rt} menuRef={promptRowMenus} />}
    </>
  )
}

/** The prompt row plus the lookups only it needs: the session record, its
 *  engine's capabilities, its engine's model catalog and the fast-mode write.
 *  A component of its own so a composer without session controls never runs them. */
function SessionPromptRow({ rt, menuRef }: { rt: SessionRuntime; menuRef?: Ref<PromptRowMenus> }) {
  const { data: sessions } = useSessions()
  const session = sessions?.find((s) => s.id === rt.sessionId)
  const caps = useEngineCapabilities(session?.engine)
  const { data: models } = useModels(session?.engine, session?.accountName)
  const setFast = useSetFastMode()
  return (
    <PromptRow
      rt={rt} session={session} caps={caps} models={models} menuRef={menuRef}
      onToggleFast={(enabled) => setFast.mutate({ id: rt.sessionId, enabled })}
      fastPending={setFast.isPending}
    />
  )
}
