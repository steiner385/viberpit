import { useCallback, useLayoutEffect, useMemo, useRef, type ReactNode } from 'react'
import type { Turn } from '@/session/reducer'
import { forkTargetFor } from '@/session/forkTarget'
import { ForkContext, type ForkActions } from './forkContext'
import { useForkFrom } from './useForkFrom'

interface Props {
  sessionId: string
  /** The live transcript's main-thread turns (`rt.state.turns`). */
  turns: readonly Turn[]
  /** `rt.state.earliestTurn` — 0 when the session's first message is loaded. */
  earliestTurn: number
  /** The engine's `fork` capability. Off: no provider value, so no fork item. */
  enabled: boolean
  children: ReactNode
}

/**
 * Provides `ForkContext` for one live session's transcript. The value is stable
 * across streaming deltas — `targetFor` reads the transcript through a ref — so a
 * new delta does not re-render every turn's menu. A menu recomputes its items
 * when it opens, which is when a stale read could matter.
 */
export function ForkProvider({ sessionId, turns, earliestTurn, enabled, children }: Props) {
  const fork = useForkFrom(sessionId)
  const transcript = useRef({ turns, earliestTurn })
  useLayoutEffect(() => { transcript.current = { turns, earliestTurn } }, [turns, earliestTurn])
  const targetFor = useCallback(
    (turn: Turn) => forkTargetFor(transcript.current.turns, turn, transcript.current.earliestTurn),
    [],
  )
  const value = useMemo<ForkActions | null>(() => (enabled ? { targetFor, fork } : null), [enabled, targetFor, fork])
  return <ForkContext.Provider value={value}>{children}</ForkContext.Provider>
}
