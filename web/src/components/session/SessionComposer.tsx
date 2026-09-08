/**
 * SessionComposer — single source of truth for wiring a live SessionRuntime into
 * the Composer. Both layouts mount it: the mobile/full-screen SessionView and the
 * desktop master-detail DesktopShell. Assembling the Composer prop bag in one place
 * stops the two call sites from drifting (a dropped `suggestions` prop on desktop is
 * exactly the bug this consolidates away).
 */

import { useMemo, useCallback } from 'react'
import { Composer } from './Composer'
import { deriveHistory } from '@/session/history'
import { useGenerateSuggestions } from '@/api/hooks'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import type { PromptContext } from '@/session/events'

interface Props {
  rt: SessionRuntime
  /** When set, every prompt sent through this composer carries the returned
   *  context (the AI assistant drawer's "sent from" chip). Computed lazily at
   *  send time (not once) so it always reflects the current route/open session. */
  contextProvider?: () => PromptContext
  /** Forward-compat: Composer has no mode-selector UI today, so this has no
   *  effect yet — accepted so the assistant drawer can opt out ahead of that UI
   *  landing, without a follow-up prop-plumbing change. */
  hideModeSelector?: boolean
  /** Prefill the draft from outside the composer (e.g. a NoticeCard's "Ask about it"). */
  externalDraft?: { text: string } | null
}

export function SessionComposer({ rt, contextProvider, hideModeSelector: _hideModeSelector, externalDraft }: Props) {
  // `hideModeSelector` is forward-compat only — Composer has no mode-selector UI to
  // hide today (see the Props doc above). Referenced here (no-op) so it isn't
  // flagged as unused; wire it up for real once that UI exists.
  void _hideModeSelector
  const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
  const genSuggestions = useGenerateSuggestions()
  const onPrompt = useCallback(
    (text: string, attachmentIds: string[]) => {
      if (contextProvider) { rt.onPrompt(text, attachmentIds, contextProvider()); return }
      rt.onPrompt(text, attachmentIds)
    },
    [rt, contextProvider],
  )
  return (
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
      suggestions={rt.state.latestSuggestions}
      onRequestSuggestions={() => genSuggestions.mutateAsync({ id: rt.sessionId })}
      history={history}
      sessionId={rt.sessionId}
      externalDraft={externalDraft}
    />
  )
}
