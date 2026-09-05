import { useSyncExternalStore } from 'react'
import { type PromptStage, advancesStage } from '@/session/promptStage'

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
 */

let flight: Record<string, PromptStage> = {}
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

/** A prompt just left the composer for `sessionId` (or was held in the browser). */
export function noteSent(sessionId: string, stage: Extract<PromptStage, 'sent' | 'buffered'>): void {
  flight = { ...flight, [sessionId]: stage }
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
  emit()
}

/** The in-flight stage for one session, or undefined. Re-renders on change. */
export function useSessionPromptFlight(sessionId: string): PromptStage | undefined {
  return useSyncExternalStore(subscribeFlight, () => flight[sessionId])
}

/** Test-only: clear all state between tests. */
export function __resetFlight(): void {
  flight = {}
  listeners.clear()
}
