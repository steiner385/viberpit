// web/src/ws/liveBatch.ts
import type { SessionEvent } from '@/session/events'

/** Per-session last-applied live sequence number, for gap detection. */
export interface GapState {
  lastSeq: Map<string, number>
}

export function createGapState(): GapState {
  return { lastSeq: new Map() }
}

/** Clear the seq baseline for a session — called on replayReset so the next live
 *  event re-baselines instead of registering a false gap. */
export function resetSeq(state: GapState, sessionId: string): void {
  state.lastSeq.delete(sessionId)
}

interface WireMeta { seq?: number; sessionId?: string }

/**
 * Filter a live batch to the active session and detect a sequence gap.
 *
 * Events without a `seq` (replay content, lifecycle frames) are always dispatched
 * and never affect gap tracking. Events tagged with a `sessionId` other than the
 * active one are dropped (a single socket can interleave multiple sessions).
 */
export function handleLiveBatch(
  events: SessionEvent[],
  ctx: { activeSessionId: string | null; state: GapState },
): { dispatch: SessionEvent[]; needsHeal: boolean } {
  const { activeSessionId, state } = ctx
  const dispatch: SessionEvent[] = []
  let needsHeal = false

  for (const e of events) {
    const meta = e as SessionEvent & WireMeta
    // Drop events explicitly owned by a different session.
    if (meta.sessionId && activeSessionId && meta.sessionId !== activeSessionId) continue

    if (typeof meta.seq === 'number' && meta.sessionId) {
      const prev = state.lastSeq.get(meta.sessionId)
      if (prev !== undefined && meta.seq !== prev + 1) needsHeal = true
      state.lastSeq.set(meta.sessionId, meta.seq)
    }
    dispatch.push(e)
  }

  return { dispatch, needsHeal }
}
