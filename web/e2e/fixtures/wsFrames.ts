/**
 * wsFrames — real current-protocol WebSocket frame builders for the e2e suite.
 *
 * Derived from the CODE, not old specs:
 *   - server/bridge/ws.py  — what the daemon/edge actually emit
 *   - web/src/ws/client.ts — `_handleInbound` (routes `batch` through handleLiveBatch)
 *   - web/src/ws/liveBatch.ts — the exact accepted shapes + the seq idempotency fence
 *   - web/src/session/reducer.ts — how frames fold into the transcript
 *
 * These replace the old dead `{type:'output', data:'…'}` vocabulary the reconnect
 * spec used to drive — a frame the client never parsed, so the test proved nothing.
 *
 * Wire ground truth (master):
 *   - Every outbound frame is coalesced by the server's batcher into a single
 *     `{type:'batch', events:[…]}` frame. replayReset, the transcript events, and
 *     replayComplete all ride INSIDE a batch's `events` array.
 *   - REPLAY frames are tagged with their owning `sessionId` (ws.py #458) but carry
 *     NO `seq` — replay is not part of the per-attach live sequence.
 *   - LIVE frames carry `sessionId` AND a per-attach `seq` (restarts at 0 each
 *     attach) — `seq` is the dedup/heal key. handleLiveBatch drops any tagged frame
 *     whose `seq` is not strictly greater than the last applied (the at-most-once
 *     "#477" fence), and drops frames tagged for a non-active session.
 *   - `rotated` is a top-level frame (not batched) with a sessionId.
 *
 * The `SessionEvent` import is type-only, so it is elided at Playwright transpile
 * time (no `@`-alias / runtime resolution needed); it keeps these builders honest
 * against the client's real event union in the editor.
 */

import type { SessionEvent, ToolKind } from '../../src/session/events'

// ── Wire-level metadata the server stamps onto batched events ─────────────────
// All optional: a bare SessionEvent already satisfies this (used for replay, which
// carries only `sessionId`). Mirrors liveBatch.ts `WireMeta`.
export interface WireMeta { sessionId?: string; seq?: number; srcSeq?: number }
export type WireEvent = SessionEvent & WireMeta

/** `{type:'batch', events}` — client.ts WsBatchMsg → handleLiveBatch. */
export interface BatchFrame { type: 'batch'; events: WireEvent[] }

/** `{type:'rotated', ...}` — client.ts WsRotatedMsg (top-level, not batched). */
export interface RotatedFrame {
  type: 'rotated'
  sessionId: string
  fromAccount: string
  toAccount: string
  reason?: string
}

// ── Atomic transcript events (plain SessionEvent shapes) ─────────────────────

export const turnStart = (turnId: string): SessionEvent => ({ type: 'turnStart', turnId })
export const textDelta = (turnId: string, text: string): SessionEvent => ({ type: 'textDelta', turnId, text })
export const turnEnd = (turnId: string): SessionEvent => ({ type: 'turnEnd', turnId })
export const userEvent = (turnId: string, text: string): SessionEvent => ({ type: 'user', turnId, text })
export const toolCall = (
  turnId: string, toolId: string, name: string, toolKind: ToolKind, input: Record<string, unknown> = {},
): SessionEvent => ({ type: 'toolCall', turnId, toolId, name, toolKind, input })

// ── Composite helpers (a full turn as the SDK would stream it) ───────────────

/** A user turn: one `user` event. */
export function userTurn(turnId: string, text: string): SessionEvent[] {
  return [userEvent(turnId, text)]
}

/** An assistant turn: turnStart → textDelta → turnEnd (what a streamed reply folds to). */
export function assistantTurn(turnId: string, text: string): SessionEvent[] {
  return [turnStart(turnId), textDelta(turnId, text), turnEnd(turnId)]
}

// ── Frame builders ───────────────────────────────────────────────────────────

/** The sessionId-tagged reset the server leads every replay batch with (ws.py). */
export const replayReset = (sessionId: string): WireEvent => ({ type: 'replayReset', sessionId })

/** The sessionId-tagged terminator that clears the loading state and reports history size. */
export const replayComplete = (sessionId: string, earliestTurn = 0, totalTurns = 0): WireEvent =>
  ({ type: 'replayComplete', sessionId, earliestTurn, totalTurns })

/** Tag every event in a list with the owning session id (as the replay path does). */
function tagAll(events: SessionEvent[], sessionId: string): WireEvent[] {
  return events.map((e) => ({ ...e, sessionId }))
}

/**
 * A transcript replay exactly as the server sends it (ws.py `_run_replay_and_complete`,
 * coalesced by the batcher): ONE batch frame led by a sessionId-tagged `replayReset`,
 * then the sessionId-tagged transcript events, then a sessionId-tagged `replayComplete`.
 *
 * The `replayReset` is the reconnect idempotency fence — it wipes rebuildable
 * transcript state and re-baselines the seq so re-delivering the same replay on a
 * reconnect re-attach rebuilds cleanly instead of double-appending
 * (reducer.ts replayReset case; liveBatch.ts resetSeq).
 */
export function replayFrame(
  sessionId: string,
  events: SessionEvent[],
  opts: { earliestTurn?: number; totalTurns?: number } = {},
): BatchFrame {
  return {
    type: 'batch',
    events: [
      replayReset(sessionId),
      ...tagAll(events, sessionId),
      replayComplete(sessionId, opts.earliestTurn ?? 0, opts.totalTurns ?? 0),
    ],
  }
}

/**
 * A LIVE batch: each event tagged with the active `sessionId` and a monotonic
 * per-attach `seq` starting at `startSeq`. Re-sending a batch whose `seq`s are not
 * strictly greater than what the client last applied is dropped by handleLiveBatch
 * (the at-most-once fence) — see the idempotency test.
 */
export function liveFrame(sessionId: string, startSeq: number, events: SessionEvent[]): BatchFrame {
  return { type: 'batch', events: events.map((e, i) => ({ ...e, sessionId, seq: startSeq + i })) }
}

export function rotated(sessionId: string, fromAccount: string, toAccount: string, reason?: string): RotatedFrame {
  return { type: 'rotated', sessionId, fromAccount, toAccount, ...(reason ? { reason } : {}) }
}

/** Serialize any frame for `ws.send`. */
export function wire(frame: BatchFrame | RotatedFrame): string {
  return JSON.stringify(frame)
}
