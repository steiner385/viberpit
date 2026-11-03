import { useCallback, useMemo, type ReactNode } from 'react'
import type { Turn } from '@/session/reducer'
import { rewindBlocked } from '@/session/rewindTarget'
import { turnMarkdown } from '@/session/turnMarkdown'
import { RewindContext, type RewindActions } from './rewindContext'
import { useRewind } from './useRewind'

/** Provides `RewindContext` for one live session (D-9) and hosts its confirm dialog.
 *  `enabled` is the engine's `rewind` capability: off, no value, so no rewind items.
 *  `status` is the live status: the items are disabled where the server would refuse
 *  (`rewindBlocked`). `send` is the composer's own send path (the runtime's
 *  `onPlainPrompt`: a busy session queues it), which Rewind & retry resends through.
 *  `onExpired` drops a divider whose undo the server called expired. */
export function RewindProvider({ sessionId, enabled, status, send, onExpired, children }: {
  sessionId: string; enabled: boolean; status: string
  send: (text: string, attachmentIds: string[]) => unknown
  onExpired?: (rewindId: string) => void; children: ReactNode
}) {
  const { start, undo, element } = useRewind(sessionId, { onExpired })
  const busy = rewindBlocked(status)
  // Rewind & retry: rewind to the failed prompt, then send its prefill unchanged — or,
  // when the server returns none, the prompt's own text. Cancel or a refusal sends nothing.
  const retry = useCallback(async (prompt: Turn) => {
    const result = await start(prompt, { edit: false })
    if (!result) return
    const text = result.prefill ?? turnMarkdown(prompt)
    if (text !== null) send(text, [])
  }, [start, send])
  const value = useMemo<RewindActions | null>(
    () => (enabled ? { start, undo, retry, busy } : null), [enabled, start, undo, retry, busy])
  return <RewindContext.Provider value={value}>{children}{element}</RewindContext.Provider>
}
