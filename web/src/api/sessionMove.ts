// "Move to host…" (D-17): the pieces every surface shares while a session is between
// hosts. Small and hook-free on purpose — the global mutation toast, the session card,
// the header and the composer all read it, so it ships in the entry bundle; the
// dialog's hooks live in the lazy `moveHooks.ts`.
import { apiErrorCode } from './client'

/** The edge guard's code: a per-session call that would build a driver arrived while
 *  the session is being moved (HTTP 409, WS error frame). */
export const SESSION_MOVING = 'SESSION_MOVING'

/** THE "Moving to <host>…" sentence — the chip, the composer and every refused action
 *  say the same thing (the WS attach frame's message is the server's copy of it). */
export function movingLabel(host: string): string {
  return `Moving to ${host}…`
}

/**
 * The message for a 409 `SESSION_MOVING` from any action, or null for any other
 * error. The server's own `message` ("This session is moving to rocky.") is fine
 * prose but differs from the chip; this keeps one sentence across the app.
 */
export function sessionMovingMessage(error: unknown): string | null {
  // Always a 409; checking the status first keeps every other error off the decoder.
  if ((error as { status?: unknown } | null)?.status !== 409) return null
  if (apiErrorCode(error) !== SESSION_MOVING) return null
  const detail = (error as { body?: { detail?: { movingTo?: unknown } } }).body?.detail
  return typeof detail?.movingTo === 'string' ? movingLabel(detail.movingTo) : null
}
