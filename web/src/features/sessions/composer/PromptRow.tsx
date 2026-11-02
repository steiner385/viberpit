/**
 * PromptRow — the compact control row under the session composer (sub-spec D §3):
 * Mode ▾ today; the model / effort pickers and the usage ring join it later. It
 * reads the live runtime directly, and the session record + engine capabilities
 * are passed in so capability-gated controls can hide (never disable) themselves.
 */

import { useImperativeHandle, useState, type Ref } from 'react'
import type { AgentCapabilities, Session } from '@/api/types'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { SESSION_MODE_OPTIONS } from '@/session/permissionModes'
import { ModePicker } from './ModePicker'

/** Imperative openers for the row's menus (the Alt+Shift+M / I / E shortcuts). */
export interface PromptRowMenus {
  openMode(): void
  openModel(): void
  openEffort(): void
}

export interface PromptRowProps {
  rt: Pick<SessionRuntime, 'mode' | 'pendingMode' | 'onSetMode'>
  /** The open session's record (undefined until the list query answers). */
  session?: Pick<Session, 'engine'> & Partial<Session>
  /** The session engine's capabilities; undefined means "unknown" — treat as present. */
  caps?: AgentCapabilities
  menuRef?: Ref<PromptRowMenus>
  /** The assistant drawer has no session controls: the whole row is hidden. */
  hideSessionControls?: boolean
}

// `session` / `caps` are not read yet: no engine restricts permission modes
// (AgentCapabilities has no mode field and Codex maps all seven). They are part
// of the row's contract for the model/effort pickers and the usage ring.
export function PromptRow({ rt, menuRef, hideSessionControls = false }: PromptRowProps) {
  const [modeOpen, setModeOpen] = useState(false)
  useImperativeHandle(menuRef, () => ({
    openMode: () => setModeOpen(true),
    openModel: () => {},
    openEffort: () => {},
  }), [])

  if (hideSessionControls) return null
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 pb-1 text-xs" data-testid="prompt-row">
      <ModePicker
        mode={rt.mode}
        pendingMode={rt.pendingMode}
        options={SESSION_MODE_OPTIONS}
        onSelect={rt.onSetMode}
        open={modeOpen}
        onOpenChange={setModeOpen}
      />
    </div>
  )
}
