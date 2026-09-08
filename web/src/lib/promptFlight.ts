import { useSyncExternalStore } from 'react'
import { type PromptStage, advancesStage } from '@/session/promptStage'
import { RESTING_BEFORE_TURN } from '@/session/statusTier'
import { revIsNewer } from '@/session/reconcile'
import type { Session } from '@/api/types'

/**
 * Which sessions have a prompt in flight, and how far it has got.
 *
 * The transcript's pending bubble answers "did my prompt land?" only while that
 * session is on screen. Fire a prompt and navigate away — or work from the session
 * list on a phone — and the card sat at a calm "Ready" for the entire 30 s–2 min
 * wait, which reads as "nothing happened".
 *
 * Written by the SOCKET (ws/client.ts), not by a component, precisely so it does not
 * depend on anything being mounted. Same module-store + useSyncExternalStore shape as
 * lib/pendingSessions.ts — the established pattern here for optimistic state that has
 * to outlive a view.
 *
 * ## How an entry ends
 *
 * Two signals, because the first one is lossy:
 *
 * 1. A NON-RESTING status for the session (`in_progress`, `needs_attention`, …): the
 *    turn started, or the session went away. Delivered by the live `status` frame.
 * 2. The session's hand-back clock (`Session.updatedAt` — server `last_turn_at`,
 *    which advances ONLY when the agent hands output back, never on an incidental
 *    row write) moved past what it was when the prompt was sent. That means the
 *    turn we sent has come and gone, even if we never saw it start.
 *
 * (2) exists because (1) alone stranded pills: close the laptop, ride out a deploy
 * restart, or reload while the turn runs, and the next thing the browser sees — a
 * `done` status frame, or the 5 s poll — is a RESTING status, which (1) rightly
 * ignores (the idle/done broadcasts DURING a wake-up must not blank the pill). Every
 * card prompted before the gap then read "Sent — starting…" until a page reload.
 * The poll carries `updatedAt`, so (2) survives any socket gap.
 */

let flight: Record<string, PromptStage> = {}
/**
 * `Session.updatedAt` as it was when the prompt was sent, per session. `null` = the
 * session had never handed back (so ANY later hand-back is an advance); absent = the
 * sessions cache was not loaded at send time, so there is no basis to judge by clock
 * and only signal (1) applies.
 */
let baselines: Record<string, string | null> = {}
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function subscribeFlight(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Stable reference between mutations — required by useSyncExternalStore. */
export function getFlight(): Record<string, PromptStage> {
  return flight
}

/**
 * A prompt just left the composer for `sessionId` (or was held in the browser).
 * `baselineUpdatedAt` is the session row's hand-back clock at this moment (see the
 * module docstring); pass `undefined` when the row is not cached.
 */
export function noteSent(
  sessionId: string,
  stage: Extract<PromptStage, 'sent' | 'buffered'>,
  baselineUpdatedAt?: string | null,
): void {
  flight = { ...flight, [sessionId]: stage }
  if (baselineUpdatedAt === undefined) {
    const rest = { ...baselines }
    delete rest[sessionId]
    baselines = rest
  } else {
    baselines = { ...baselines, [sessionId]: baselineUpdatedAt }
  }
  emit()
}

/**
 * An ack moved the prompt along. Ignores a session with nothing in flight (an ack for
 * a prompt sent by ANOTHER browser tab is not ours to narrate) and never demotes —
 * acks and echoes race on one socket.
 */
export function noteStage(sessionId: string, stage: PromptStage): void {
  const cur = flight[sessionId]
  if (cur === undefined || !advancesStage(cur, stage)) return
  flight = { ...flight, [sessionId]: stage }
  emit()
}

/** Force a stage regardless of direction — the one legitimate backwards move, a
 *  server refusal returning the prompt to the browser's hold queue. */
export function noteHeld(sessionId: string): void {
  if (flight[sessionId] === undefined || flight[sessionId] === 'buffered') return
  flight = { ...flight, [sessionId]: 'buffered' }
  emit()
}

/** The turn is confirmed (or the session went away) — stop narrating the send. */
export function clearFlight(sessionId: string): void {
  if (flight[sessionId] === undefined) return
  const rest = { ...flight }
  delete rest[sessionId]
  flight = rest
  if (sessionId in baselines) {
    const b = { ...baselines }
    delete b[sessionId]
    baselines = b
  }
  emit()
}

/** The slice of a session row the reconcile reads — a live `status` frame and a
 *  polled row both satisfy it. */
export type FlightRow = Pick<Session, 'id' | 'status'> & { updatedAt?: string | null }

/** True when this row is proof the sent prompt's turn is no longer "on its way". */
function rowSupersedesFlight(row: FlightRow): boolean {
  if (!RESTING_BEFORE_TURN.has(row.status)) return true
  const baseline = baselines[row.id]
  if (baseline === undefined) return false
  // revIsNewer: absent candidate → false (a frame with no clock proves nothing);
  // null baseline + real clock → true (the first-ever hand-back on a never-run session).
  return revIsNewer(row.updatedAt, baseline)
}

/** One row of fresh session news (a live status frame). Clears the entry when the
 *  row proves the turn is past — see the module docstring for the two signals. */
export function reconcileFlightRow(row: FlightRow): void {
  if (flight[row.id] === undefined) return
  if (rowSupersedesFlight(row)) clearFlight(row.id)
}

/** The same, over a whole poll result. Sessions absent from `rows` are left alone —
 *  the list may be filtered, and absence is not evidence about the turn. */
export function reconcileFlight(rows: readonly FlightRow[]): void {
  for (const row of rows) reconcileFlightRow(row)
}

/** The in-flight stage for one session, or undefined. Re-renders on change. */
export function useSessionPromptFlight(sessionId: string): PromptStage | undefined {
  return useSyncExternalStore(subscribeFlight, () => flight[sessionId])
}

/** Test-only: clear all state between tests. */
export function __resetFlight(): void {
  flight = {}
  baselines = {}
  listeners.clear()
}
