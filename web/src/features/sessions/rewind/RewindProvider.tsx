import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { Turn } from '@/session/reducer'
import { rewindBlocked } from '@/session/rewindTarget'
import { turnMarkdown } from '@/session/turnMarkdown'
import { RewindContext, type RewindActions } from './rewindContext'
import { useRewind } from './useRewind'

/** How long Rewind & retry waits for the `rewound` cut before sending anyway — a view
 *  that does not hold the target never places the marker, it replays instead. */
const RETRY_CUT_WAIT_MS = 1500

/** Provides `RewindContext` for one live session (D-9) and hosts its confirm dialog.
 *  `enabled` is the engine's `rewind` capability: off, no value, so no rewind items.
 *  `status` is the live status: the items are disabled where the server would refuse
 *  (`rewindBlocked`). `send` is the composer's own send path (the runtime's
 *  `onPlainPrompt`: a busy session queues it), which Rewind & retry resends through.
 *  `appliedRewindId` is the reducer's rewind marker (`state.rewind?.rewindId`): a retry
 *  sends once its own cut is applied, so the resent bubble lands below the cut, not in
 *  the turns it is about to drop. `onExpired` drops a divider whose undo the server
 *  called expired. */
export function RewindProvider({ sessionId, enabled, status, send, appliedRewindId = null, onExpired, children }: {
  sessionId: string; enabled: boolean; status: string
  send: (text: string, attachmentIds: string[]) => unknown
  appliedRewindId?: string | null
  onExpired?: (rewindId: string) => void; children: ReactNode
}) {
  const { start, undo, element, active } = useRewind(sessionId, { onExpired })
  const busy = rewindBlocked(status)
  const [waiting, setWaiting] = useState(false)

  // The cut signal: resolve the retry waiting on `appliedRewindId` when it arrives.
  const applied = useRef(appliedRewindId)
  const waiter = useRef<{ rewindId: string; resolve: () => void } | null>(null)
  useEffect(() => {
    applied.current = appliedRewindId
    if (waiter.current && waiter.current.rewindId === appliedRewindId) waiter.current.resolve()
  }, [appliedRewindId])
  const cutApplied = useCallback((rewindId: string) => new Promise<void>((resolve) => {
    if (applied.current === rewindId) { resolve(); return }
    const done = () => { clearTimeout(timer); waiter.current = null; resolve() }
    const timer = setTimeout(done, RETRY_CUT_WAIT_MS)
    waiter.current = { rewindId, resolve: done }
  }), [])

  // Rewind & retry: rewind to the failed prompt, then send its prefill unchanged — or,
  // when the server returns none, the prompt's own text. Cancel or a refusal sends nothing.
  const retry = useCallback(async (prompt: Turn) => {
    const result = await start(prompt, { edit: false, retry: true })
    if (!result) return
    setWaiting(true)
    try {
      await cutApplied(result.rewindId)
    } finally {
      setWaiting(false)
    }
    const text = result.prefill ?? turnMarkdown(prompt)
    if (text !== null) send(text, [])
  }, [start, send, cutApplied])
  const open = active || waiting
  const value = useMemo<RewindActions | null>(
    () => (enabled ? { start, undo, retry, busy, open } : null), [enabled, start, undo, retry, busy, open])
  return <RewindContext.Provider value={value}>{children}{element}</RewindContext.Provider>
}
