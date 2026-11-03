import { useMemo, type ReactNode } from 'react'
import { RewindContext, type RewindActions } from './rewindContext'
import { useRewind } from './useRewind'

/** Provides `RewindContext` for one live session (D-9) and hosts its confirm dialog.
 *  `enabled` is the engine's `rewind` capability: off, no value, so no rewind items. */
export function RewindProvider({ sessionId, enabled, busy, children }: {
  sessionId: string; enabled: boolean; busy: boolean; children: ReactNode
}) {
  const { start, undo, element } = useRewind(sessionId)
  const value = useMemo<RewindActions | null>(
    () => (enabled ? { start, undo, busy } : null), [enabled, start, undo, busy])
  return <RewindContext.Provider value={value}>{children}{element}</RewindContext.Provider>
}
