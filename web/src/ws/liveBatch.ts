// web/src/ws/liveBatch.ts
import type { SessionEvent } from '@/session/events'

/** Per-session live-sequence baselines for gap detection.
 *
 *  `lastSeq` is the per-attach seq that drives dedup + self-heal (the client's
 *  correctness key). `lastSrcSeq` is the daemon's end-to-end SOURCE seq (`srcSeq`,
 *  audit W2.3) — DIAGNOSTIC ONLY: a hole in it is recorded as loss telemetry but
 *  never triggers dedup, heal, or a dropped frame. */
export interface GapState {
  lastSeq: Map<string, number>
  lastSrcSeq: Map<string, number>
}

export function createGapState(): GapState {
  return { lastSeq: new Map(), lastSrcSeq: new Map() }
}

/** Clear both seq baselines for a session — called on replayReset so the next live
 *  event re-baselines instead of registering a false gap (applies to the source-seq
 *  telemetry too, so a reconnect replay doesn't emit a spurious srcSeq gap). */
export function resetSeq(state: GapState, sessionId: string): void {
  state.lastSeq.delete(sessionId)
  state.lastSrcSeq.delete(sessionId)
}

/** A non-contiguous daemon source seq observed on the live stream — pure telemetry
 *  (audit W2.3), surfaced for the caller to log; it never affects dispatch/heal. */
export interface SrcSeqGap { sessionId: string; prev: number; got: number }

// `seq` is the per-attach dedup/heal key; `srcSeq` is the daemon's additive
// end-to-end source seq (loss telemetry only — see GapState).
interface WireMeta { seq?: number; sessionId?: string; srcSeq?: number }

/**
 * Filter a live batch to the active session and detect a sequence gap.
 *
 * Events without a `seq` (replay content, lifecycle frames) are always dispatched
 * and never affect gap tracking. Events tagged with a `sessionId` other than the
 * active one are dropped (a single socket can interleave multiple sessions).
 *
 * Wire fields: `seq` is the per-attach dedup/heal key (restarts at 0 each attach).
 * `srcSeq` (optional, remote mode only — audit W2.3) is the daemon's end-to-end
 * SOURCE seq that survives re-attach; a hole in it is returned in `srcSeqGaps` as
 * pure loss telemetry and NEVER sets `needsHeal` or drops a frame.
 *
 * Idempotency contract for frames carrying both `seq` and `sessionId`: a frame
 * whose `seq` is not strictly greater than the last-applied seq for that session
 * (a duplicate or stale retransmit, e.g. after a reconnect) is dropped here and
 * never reaches `dispatch` — the reducer may assume at-most-once delivery of
 * tagged frames and never needs to de-duplicate. An exact re-send (`seq === prev`)
 * needs no heal (dropping it is the fix); a regression (`seq < prev`) means the
 * stream itself is confused, so it still flags `needsHeal`.
 *
 * A `replayReset` re-baselines its session's seq — applied HERE, in frame order,
 * not via a caller pre-scan: the server's single outbound FIFO can flush a stale
 * old-epoch event and the fresh replayReset in the SAME batch, stale FIRST
 * (detach→attach while the batcher held pre-detach frames — the stale-straddle
 * shape). Single-pass processing lets the stale seq land and THEN be wiped by
 * the reset, so the new epoch's seq 0 is accepted; a pre-scan reset would let
 * the stale seq re-poison the fresh baseline → false gap → heal loop mid-turn.
 * (The FIFO also guarantees stale frames precede the reset, so the duplicate-drop
 * rule above never eats a fresh post-reset frame.)
 */
export function handleLiveBatch(
  events: SessionEvent[],
  ctx: { activeSessionId: string | null; state: GapState },
): { dispatch: SessionEvent[]; needsHeal: boolean; srcSeqGaps: SrcSeqGap[] } {
  const { activeSessionId, state } = ctx
  const dispatch: SessionEvent[] = []
  const srcSeqGaps: SrcSeqGap[] = []
  let needsHeal = false

  for (const e of events) {
    const meta = e as SessionEvent & WireMeta
    // Re-baseline on replayReset BEFORE the ownership filter so a reset tagged
    // for a non-viewed session still clears THAT session's stale baseline
    // (falling back to the active session for legacy untagged frames).
    if ((e as { type?: string }).type === 'replayReset') {
      const sid = meta.sessionId ?? activeSessionId
      if (sid) resetSeq(state, sid)
    }
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

    // srcSeq loss telemetry (audit W2.3) — purely ADDITIVE: a non-contiguous daemon
    // source seq is recorded for the caller to log, but NEVER sets needsHeal or drops
    // the frame. Runs only for frames that pass the dedup above (a duplicate/stale
    // retransmit already `continue`d), so a retransmit can't register a false gap.
    if (typeof meta.srcSeq === 'number' && meta.sessionId) {
      const prevSrc = state.lastSrcSeq.get(meta.sessionId)
      if (prevSrc !== undefined && meta.srcSeq !== prevSrc + 1) {
        srcSeqGaps.push({ sessionId: meta.sessionId, prev: prevSrc, got: meta.srcSeq })
      }
      state.lastSrcSeq.set(meta.sessionId, meta.srcSeq)
    }
    dispatch.push(e)
  }

  return { dispatch, needsHeal, srcSeqGaps }
}
