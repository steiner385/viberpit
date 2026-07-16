// web/src/ws/liveBatch.ts
import type { SessionEvent } from '@/session/events'

/** Per-session live-sequence baselines for gap detection.
 *
 *  `lastSeq` is the per-attach seq that drives dedup + self-heal (the client's
 *  correctness key). `lastSrcSeq` is the daemon's end-to-end SOURCE seq (`srcSeq`,
 *  audit W2.3): DIAGNOSTIC for heal (a hole never drops a frame), but it is ALSO the
 *  wave-3 catch-up cursor — the anchor a heal/reconnect re-attach sends as
 *  `sinceSrcSeq` so the server resumes from the daemon ring instead of full-replaying.
 *  `srcContiguous` gates that: true iff NO srcSeq hole has been observed since the last
 *  replayComplete/resumeOk (established on those, reset on replayReset, cleared on a
 *  hole). A cursor is only safe to send when the source stream has been unbroken. */
export interface GapState {
  lastSeq: Map<string, number>
  lastSrcSeq: Map<string, number>
  srcContiguous: Map<string, boolean>
  /** The daemon epoch that minted the current `lastSrcSeq` cursor (#497), taken from the
   *  `epoch` field on the resumeOk/replayComplete that established it. Sent back as
   *  `sinceEpoch` alongside `sinceSrcSeq` so the server can reject a cursor from a previous
   *  daemon epoch (a restart resets the srcSeq space to 0) and force a full replay. Absent
   *  ⇒ no epoch known ⇒ server full-replays (conservative). Pairs with `lastSrcSeq`. */
  srcEpoch: Map<string, string>
}

export function createGapState(): GapState {
  return {
    lastSeq: new Map(), lastSrcSeq: new Map(), srcContiguous: new Map(),
    srcEpoch: new Map(),
  }
}

/** Reset the per-attach + source-seq BASELINES for a session — called on replayReset
 *  and resumeOk so the next live event re-baselines instead of registering a false gap
 *  (applies to the source-seq telemetry too, so a reconnect replay / ring resume doesn't
 *  emit a spurious srcSeq gap). Deliberately does NOT touch `srcContiguous`: that flag is
 *  managed at each protocol site (replayReset clears it, replayComplete/resumeOk set it)
 *  because the two events want opposite contiguity outcomes from the same baseline reset.
 *
 *  The per-attach baseline is SEEDED to -1, not deleted: the server restarts its
 *  per-attach seq at 0 on every attach (full replay and ring resume alike), and a GAP
 *  sentinel consumes a seq precisely so the client sees a hole — but a hole BEFORE the
 *  first delivered frame is invisible against a deleted (undefined) baseline, silently
 *  swallowing leading loss (found by the cross-layer pipeline fuzz). Against -1, seq 0
 *  is contiguous and a first frame at seq>0 correctly flags a heal. */
export function resetSeq(state: GapState, sessionId: string): void {
  state.lastSeq.set(sessionId, -1)
  state.lastSrcSeq.delete(sessionId)
  // srcEpoch pairs with lastSrcSeq (it is that cursor's minting epoch), so clear it on the
  // same baseline reset (#497). The resumeOk/replayComplete that drives the reset re-sets it
  // from its own `epoch` field immediately after, so the current epoch is never lost.
  state.srcEpoch.delete(sessionId)
}

/** Invalidate the catch-up cursor for a session on a FRESH user-initiated attach.
 *  A public `attach(sessionId)` represents a brand-new view whose reducer mounts EMPTY
 *  (`key={sessionId}` remounts the runtime). The srcSeq cursor lives in the socket
 *  singleton and survives that remount, so a surviving `lastSrcSeq`/`srcContiguous` for
 *  this session is now STALE — a reopen re-attach that sent it as `sinceSrcSeq` would get
 *  `resumeOk` + the ring tail only against an empty transcript (nav-return tail-only bug).
 *  Clearing both forces `catchUpCursor` → null → the server full-replays from the durable
 *  transcript. Per-session by construction (Map deletes keyed on `sessionId`), so other
 *  sessions' cursors are untouched. Distinct from `resetSeq`: this DOES clear
 *  `srcContiguous` (the whole point — forbid catch-up), where a mid-stream reset must not. */
export function invalidateCatchUp(state: GapState, sessionId: string): void {
  state.lastSrcSeq.delete(sessionId)
  state.srcContiguous.delete(sessionId)
  // Drop the cursor's minting epoch too (#497) — a stale epoch would otherwise be sent as
  // sinceEpoch on the reopen re-attach; clearing it forces the full-replay default.
  state.srcEpoch.delete(sessionId)
}

/** The srcSeq cursor to send as `sinceSrcSeq` on a heal / reconnect re-attach, or null
 *  when a full replay is required. Returns `lastSrcSeq` ONLY when the source stream has
 *  stayed contiguous since the last replayComplete/resumeOk (no observed srcSeq hole) AND
 *  a lastSrcSeq exists. Any observed loss (or a not-yet-established baseline) → null → the
 *  client omits the cursor and the server full-replays from the durable transcript. */
export function catchUpCursor(state: GapState, sessionId: string): number | null {
  if (state.srcContiguous.get(sessionId) !== true) return null
  const last = state.lastSrcSeq.get(sessionId)
  return typeof last === 'number' ? last : null
}

/** The daemon epoch that minted the current catch-up cursor (#497), to send as `sinceEpoch`
 *  alongside `sinceSrcSeq`. Null when unknown — no `epoch` was carried on the frame that
 *  established the cursor (e.g. local/in-process mode), in which case the server rejects the
 *  cursor and full-replays. Only meaningful together with a non-null `catchUpCursor`. */
export function catchUpEpoch(state: GapState, sessionId: string): string | null {
  const e = state.srcEpoch.get(sessionId)
  return typeof e === 'string' ? e : null
}

/** A non-contiguous daemon source seq observed on the live stream — pure telemetry
 *  (audit W2.3), surfaced for the caller to log; it never affects dispatch/heal. */
export interface SrcSeqGap { sessionId: string; prev: number; got: number }

// `seq` is the per-attach dedup/heal key; `srcSeq` is the daemon's additive
// end-to-end source seq (loss telemetry only — see GapState).
interface WireMeta { seq?: number; sessionId?: string; srcSeq?: number; epoch?: string }

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
    const etype = (e as { type?: string }).type
    // Re-baseline on replayReset BEFORE the ownership filter so a reset tagged
    // for a non-viewed session still clears THAT session's stale baseline
    // (falling back to the active session for legacy untagged frames).
    if (etype === 'replayReset') {
      const sid = meta.sessionId ?? activeSessionId
      if (sid) {
        resetSeq(state, sid)
        // A full replay is starting: the source stream is NOT contiguous until it
        // finishes (replayComplete re-establishes the cursor baseline).
        state.srcContiguous.set(sid, false)
      }
    }
    // resumeOk (wave-3 cursor catch-up): the server resumed the live stream from our
    // srcSeq cursor instead of full-replaying. Re-baseline the per-attach + srcSeq
    // trackers (the ring tail restarts the per-attach seq at 0) and mark the source
    // contiguous — the transcript simply CONTINUES, so it is NOT dispatched to the
    // reducer (no replayReset, no stale-buffer cycle, no transcript-state change).
    if (etype === 'resumeOk') {
      const sid = meta.sessionId ?? activeSessionId
      if (sid) {
        resetSeq(state, sid)
        state.srcContiguous.set(sid, true)
        // Re-anchor the cursor's epoch to the one the server resumed us under (#497). The
        // resetSeq above cleared it; set it fresh (or leave cleared when no epoch is carried
        // — an old server / local mode — so the next cursor is sent without a stale epoch).
        if (typeof meta.epoch === 'string') state.srcEpoch.set(sid, meta.epoch)
      }
      continue
    }
    // replayComplete ends a full replay: from here the source stream is a clean
    // baseline, so a future heal/reconnect may catch up from its cursor. (Still
    // dispatched below — the reducer needs it to clear `replaying`.)
    if (etype === 'replayComplete') {
      const sid = meta.sessionId ?? activeSessionId
      if (sid) {
        state.srcContiguous.set(sid, true)
        // Remember the epoch this fresh baseline was minted under (#497) so a later
        // heal/reconnect echoes it as sinceEpoch. Absent (local mode / old server) ⇒ clear
        // it, so the cursor is sent without an epoch and the server full-replays.
        if (typeof meta.epoch === 'string') state.srcEpoch.set(sid, meta.epoch)
        else state.srcEpoch.delete(sid)
      }
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
        // A source-seq hole means events were lost between daemon and browser: the
        // cursor is no longer trustworthy, so forbid catch-up until a full replay (or a
        // fresh resumeOk) re-establishes contiguity. This is the browser-side guard that
        // pairs with the server's ring_covers `since <= head` check.
        state.srcContiguous.set(meta.sessionId, false)
      }
      state.lastSrcSeq.set(meta.sessionId, meta.srcSeq)
    }
    dispatch.push(e)
  }

  return { dispatch, needsHeal, srcSeqGaps }
}
