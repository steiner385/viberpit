/**
 * SessionComposer — single source of truth for wiring a live SessionRuntime into
 * the Composer. Both layouts mount it: the mobile/full-screen SessionView and the
 * desktop master-detail DesktopShell. Assembling the Composer prop bag in one place
 * stops the two call sites from drifting (a dropped `suggestions` prop on desktop is
 * exactly the bug this consolidates away).
 */

import { useMemo, useCallback, type Ref } from 'react'
import { Composer } from './Composer'
import type { MentionSource } from './MentionMenu'
import { PromptRow, type PromptRowMenus } from './PromptRow'
import { deriveHistory } from '@/session/history'
import { useEngineCapabilities, useGenerateSuggestions, useSessions } from '@/api/hooks'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { PromptContext } from '@/session/events'

interface Props {
  rt: SessionRuntime
  /** When set, every prompt sent through this composer carries the returned
   *  context (the AI assistant drawer's "sent from" chip). Computed lazily at
   *  send time (not once) so it always reflects the current route/open session. */
  contextProvider?: () => PromptContext
  /** Hide the prompt row (mode picker and friends) — the AI assistant drawer
   *  has no session controls. */
  hideSessionControls?: boolean
  /** Imperative openers for the prompt row's menus (keyboard shortcuts). */
  promptRowMenus?: Ref<PromptRowMenus>
  /** Prefill the draft from outside the composer (e.g. a NoticeCard's "Ask about it"). */
  externalDraft?: { text: string } | null
  /** `@` mentions the composer can insert (spec v2 §10.2). Only the AI assistant
   *  drawer supplies this — see Composer's own doc on the prop. */
  mentionSource?: MentionSource[]
}

export function SessionComposer({ rt, contextProvider, hideSessionControls = false, promptRowMenus, externalDraft, mentionSource }: Props) {
  const { data: sessions } = useSessions()
  const session = sessions?.find((s) => s.id === rt.sessionId)
  const caps = useEngineCapabilities(session?.engine)
  const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
  const genSuggestions = useGenerateSuggestions()
  const onPrompt = useCallback(
    (text: string, attachmentIds: string[], context?: PromptContext, replySource?: 'suggested') => {
      if (replySource) { rt.onPrompt(text, attachmentIds, contextProvider?.() ?? context, replySource); return }
      if (contextProvider) { rt.onPrompt(text, attachmentIds, contextProvider()); return }
      rt.onPrompt(text, attachmentIds)
    },
    [rt, contextProvider],
  )
  return (
    <>
      <Composer
        status={rt.state.status}
        onPrompt={onPrompt}
        onActivity={rt.onActivity}
        onInterrupt={rt.onInterrupt}
        onInterruptAndPrompt={rt.onInterruptAndPrompt}
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
        mentionSource={mentionSource}
      />
      <PromptRow rt={rt} session={session} caps={caps} menuRef={promptRowMenus} hideSessionControls={hideSessionControls} />
    </>
  )
}
