/**
 * `/rewind` (D-9): the dock composer's onCommand opens a picker of the session's recent
 * prompts (newest first, at most 20) over the composer, and picking one runs Rewind to
 * here. Where the server would refuse (`rewindBlocked`: a turn running, a card
 * pending), or with no prompt to rewind to, it toasts and opens nothing. Only the
 * catalog's `client` entry is ours (the server lists it only where the engine can
 * rewind), so anything else — and a `rewind` that is not ours — goes to the session.
 */
import { createElement, Fragment, useCallback, useState, type ReactNode } from 'react'
import type { Turn } from '@/session/reducer'
import { recentRewindTargets, rewindBlocked } from '@/session/rewindTarget'
import { turnMarkdown } from '@/session/turnMarkdown'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { RewindPicker, type PickerItem } from './RewindPicker'
import { loadRewindFlow, useRewind } from './useRewind'

export interface RewindCommand {
  onCommand: SessionRuntime['onCommand']
  /** The picker and confirm dialog while open — render beside the composer. */
  element: ReactNode
}

export function useRewindCommand(rt: SessionRuntime): RewindCommand {
  const { start, element: dialog } = useRewind(rt.sessionId)
  const [picking, setPicking] = useState<PickerItem[] | null>(null)
  const { commands, onCommand, state: { turns, status } } = rt
  const ours = commands.some((c) => c.name === 'rewind' && c.kind === 'client')
  const busy = rewindBlocked(status.state)
  const handle = useCallback((name: string, args: string) => {
    if (name !== 'rewind' || !ours) { onCommand(name, args); return }
    const targets = recentRewindTargets(turns)
    if (busy || targets.length === 0) void loadRewindFlow().then((m) => m.toastNoPicker(busy))
    else setPicking(targets.map((turn) => ({ turn, text: turnMarkdown(turn) ?? '' })))
  }, [ours, onCommand, turns, busy])
  const pick = useCallback((turn: Turn) => { setPicking(null); void start(turn, { edit: false }) }, [start])
  const element = createElement(Fragment, null, dialog,
    picking && createElement(RewindPicker, { items: picking, onPick: pick, onClose: () => setPicking(null) }))
  return { onCommand: handle, element }
}
