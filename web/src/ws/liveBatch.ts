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
 *
 * Idempotency contract for frames carrying both `seq` and `sessionId`: a frame
 * whose `seq` is not strictly greater than the last-applied seq for that session
 * (a duplicate or stale retransmit, e.g. after a reconnect) is dropped here and
 * never reaches `dispatch` — the reducer may assume at-most-once delivery of
 * tagged frames and never needs to de-duplicate. An exact re-send (`seq === prev`)
 * needs no heal (dropping it is the fix); a regression (`seq < prev`) means the
 * stream itself is confused, so it still flags `needsHeal`.
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
      if (prev !== undefined && meta.seq <= prev) {
        // Duplicate or stale retransmit — drop it. A regression (seq < prev)
        // means the stream is confused and still needs a heal; an exact
        // re-send (seq === prev) is a harmless duplicate, handled silently.
        if (meta.seq < prev) needsHeal = true
        continue
      }
      if (prev !== undefined && meta.seq !== prev + 1) needsHeal = true
      state.lastSeq.set(meta.sessionId, meta.seq)
    }
    dispatch.push(e)
  }

  return { dispatch, needsHeal }
}
