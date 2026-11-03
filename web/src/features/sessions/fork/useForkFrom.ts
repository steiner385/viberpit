/**
 * The "Fork from here" flow (D-8), shared by the message menu and `/fork`: POST the
 * fork, seed the child's composer with the prefill, open the child, and say what
 * happened — or one error toast with the mapped message. The copy (`forkCopy`) is
 * imported on first use, which keeps it off the entry chunk (bundle budget).
 */
import { useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { useForkSession } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { seedSessionDraft } from '@/features/sessions/composer/useSessionDraft'
import { latestReplyTarget, type ForkTarget } from '@/session/forkTarget'
import { sessionPath } from '@/session/sessionNavState'
import type { SessionRuntime } from '@/session/useSessionRuntime'

const loadCopy = () => import('./forkCopy')

/** `fork(target)` for session `sessionId`. A second call while one is in flight
 *  is ignored: each call is its own fork (its own `requestId`), so a double click
 *  would otherwise make two children. */
export function useForkFrom(sessionId: string): (target: ForkTarget) => Promise<void> {
  const { fork } = useForkSession(sessionId)
  const navigate = useNavigate()
  const inFlight = useRef(false)
  return useCallback(async (target: ForkTarget) => {
    if (inFlight.current) return
    inFlight.current = true
    // Started with the request, so the copy is there when the answer is.
    const copy = loadCopy()
    try {
      const { sessionId: childId, prefill } = await fork(target)
      // Before navigating: the child's composer reads its draft once, on mount.
      if (prefill !== null) seedSessionDraft(childId, prefill)
      navigate(sessionPath(childId))
      toast((await copy).FORK_SUCCESS)
    } catch (err) {
      const { FORK_ERROR_MESSAGES, FORK_FAILED_FALLBACK } = await copy
      const code = apiErrorCode(err)
      toast.error((code !== null ? FORK_ERROR_MESSAGES[code] : undefined)
        ?? extractMutationErrorMessage(err, FORK_FAILED_FALLBACK))
    } finally {
      inFlight.current = false
    }
  }, [fork, navigate])
}

/**
 * `rt.onCommand` with the app-owned `/fork` handled here: it forks at the latest
 * settled reply, or toasts and sends nothing when there is none. Only the
 * catalog's `client` entry is ours — the server offers it only where the engine
 * can fork — so any other `fork` (a vendor passthrough) goes to the session.
 */
export function useForkCommand(rt: SessionRuntime): SessionRuntime['onCommand'] {
  const forkFrom = useForkFrom(rt.sessionId)
  const { commands, onCommand, state: { turns } } = rt
  const ours = commands.some((c) => c.name === 'fork' && c.kind === 'client')
  return useCallback((name: string, args: string) => {
    if (name !== 'fork' || !ours) { onCommand(name, args); return }
    const target = latestReplyTarget(turns)
    if (target) void forkFrom(target)
    else void loadCopy().then((copy) => toast(copy.FORK_NOTHING_TO_FORK))
  }, [ours, onCommand, turns, forkFrom])
}
