/**
 * SessionComposer — single source of truth for wiring a live SessionRuntime into
 * the Composer. Both layouts mount it: the mobile/full-screen SessionView and the
 * desktop master-detail DesktopShell. Assembling the Composer prop bag in one place
 * stops the two call sites from drifting (a dropped `suggestions` prop on desktop is
 * exactly the bug this consolidates away).
 */

import { useMemo } from 'react'
import { Composer } from './Composer'
import { deriveHistory } from '@/session/history'
import type { SessionRuntime } from '@/session/useSessionRuntime'

export function SessionComposer({ rt }: { rt: SessionRuntime }) {
  const history = useMemo(() => deriveHistory(rt.state.turns), [rt.state.turns])
  return (
    <Composer
      status={rt.state.status}
      onPrompt={rt.onPrompt}
      onInterrupt={rt.onInterrupt}
      queued={rt.state.queued}
      onCancelQueued={rt.onCancelQueued}
      chatArmed={rt.chatArmed}
      commands={rt.commands}
      onCommand={rt.onCommand}
      autoFocusKey={rt.sessionId}
      suggestions={rt.state.latestSuggestions}
      history={history}
      onRequestSuggestions={rt.onRequestSuggestions}
    />
  )
}
