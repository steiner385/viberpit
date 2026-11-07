/**
 * What the sandbox (D-23) says — THE one table of its copy, shared by the Environment
 * card's row (`rail/SandboxControls`), the hold banner's actions
 * (`panels/SandboxHoldActions`) and Settings › Sessions.
 *
 * The codes are the server's (`server/sessions/session_actions.py:sandbox_action_error`,
 * plus the person-only guard, `SESSION_MOVING` and `daemon_outdated`). A code missing
 * here falls back to the server's own message, then to the action's fallback.
 */
import { ApiRequestError, apiErrorCode } from '@/api/client'
import { sessionMovingMessage } from '@/api/sessionMove'
import type { SandboxFacts, SessionSandbox } from '@/api/types'

/** Binding (plan D-23, spike amendment 2): in full-auto the sandbox is containment only. */
export const SANDBOX_NOTE =
  'Network: GitHub only. Writes: this worktree only. In full-auto there are no prompts — the sandbox only contains commands.'

export const SANDBOX_BUSY = 'Busy — wait for the turn to finish'
export const SANDBOX_UNAVAILABLE_FALLBACK = 'The sandbox is unavailable on this host'
export const SANDBOX_TOGGLE_FALLBACK = "Couldn't change the sandbox"

/** Settings › Sessions: off by default, and what the host needs. */
export const SANDBOX_DEFAULT_COPY =
  'Off by default. New Claude sessions run their commands contained: network to GitHub only, writes to their own worktree only. Needs bubblewrap (bwrap) and socat on the host — a session whose sandbox cannot start is held, never run without it.'

/** The respawn warning, per direction. Both directions restart the session. Picked by
 *  `sandboxConfirmCopy` — the card and the hold banner share it (`SandboxConfirmDialog`). */
export const SANDBOX_CONFIRM = {
  on: {
    title: 'Turn the sandbox on?',
    description: 'This restarts the session with the sandbox on. The conversation carries on, but anything running in it now is stopped.',
    confirmLabel: 'Restart with sandbox',
  },
  off: {
    title: 'Turn the sandbox off?',
    description: 'This restarts the session without the sandbox. Its commands can then reach any network host and write outside this worktree.',
    confirmLabel: 'Restart without sandbox',
  },
  continueOff: {
    title: 'Continue without the sandbox?',
    description: 'This restarts the session without the sandbox, so its commands are no longer contained: they can reach any network host and write outside this worktree.',
    confirmLabel: 'Turn off and continue',
  },
} as const

/** Which warning a toggle shows: turning off a HELD session is "continue without it",
 *  from the card and the hold banner alike. */
export function sandboxConfirmCopy(enabled: boolean, held: boolean) {
  if (enabled) return SANDBOX_CONFIRM.on
  return held ? SANDBOX_CONFIRM.continueOff : SANDBOX_CONFIRM.off
}

/** A 403 `SESSION_CONTROL_HUMAN_ONLY` with no server message (the toggle and the
 *  Settings default). The server's own message is preferred: it names the action and,
 *  for a request from the ViberPit host itself, says to use another device. */
export const SANDBOX_HUMAN_ONLY =
  "Only a person using ViberPit can change the sandbox — a token or an agent can't. If this browser runs on the ViberPit host itself, use another device on your tailnet."

/** The kill switch is off on this host: nothing can turn the sandbox on. */
export const SANDBOX_SWITCHED_OFF = 'The sandbox is turned off on this server'

/** The disabled toggle's "Re-check host" failed. */
export const SANDBOX_RECHECK_FAILED = "Couldn't re-check the host"

/** The 200 a respawn answers when its sandbox failed: the session is now held. */
export const SANDBOX_DID_NOT_START = "The sandbox didn't start, so the session is held. Retry, or turn the sandbox off to continue."

export const SANDBOX_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  SANDBOX_DISABLED: `${SANDBOX_SWITCHED_OFF} — turn it off for this session to continue`,
  SANDBOX_UNSUPPORTED: "This session's agent can't run in the sandbox",
  SANDBOX_SESSION_BUSY: 'The session is busy — wait for the turn to finish, answer any open card, and let background tasks end',
  SANDBOX_SESSION_ENDED: 'This session has ended, so its sandbox cannot change',
  SESSION_MOVING: 'The session is moving to another host — try again once it has landed',
  daemon_outdated: 'The session runner needs an update before it can do this — try after the next rollout',
}

/** `{detail: {code, message, reason?}}` off a failed toggle, in the table's words.
 *  `SANDBOX_UNAVAILABLE` carries the host's first failed check as `reason`. */
export function sandboxError(err: unknown, fallback: string = SANDBOX_TOGGLE_FALLBACK): string {
  if (err instanceof ApiRequestError && err.status === 404) return 'This session has already ended.'
  const detail = err instanceof ApiRequestError ? err.body?.detail : null
  const rec = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : {}
  const code = apiErrorCode(err)
  if (code === 'SESSION_CONTROL_HUMAN_ONLY') {
    return typeof rec.message === 'string' && rec.message ? rec.message : SANDBOX_HUMAN_ONLY
  }
  if (code === 'SANDBOX_UNAVAILABLE') {
    return typeof rec.reason === 'string' && rec.reason ? `${SANDBOX_UNAVAILABLE_FALLBACK}: ${rec.reason}` : SANDBOX_UNAVAILABLE_FALLBACK
  }
  return sessionMovingMessage(err)
    ?? (code !== null ? SANDBOX_ERROR_MESSAGES[code] : undefined)
    ?? (typeof rec.message === 'string' ? rec.message : null)
    ?? fallback
}

/** Off / Active / Starting… / Failed: <reason>. */
export function sandboxStateLabel(s: Pick<SessionSandbox, 'state' | 'reason'>): string {
  switch (s.state) {
    case 'active': return 'Active'
    case 'pending': return 'Starting…'
    case 'failed': return `Failed: ${s.reason || 'the sandbox did not start'}`
    default: return 'Off'
  }
}

const CANT_CHANGE = "The sandbox can't be changed right now"

/** Why the toggle is disabled, or null when it is not. `enabled` is the session's own
 *  value (the toggle's direction); `busy` the live turn state, fresher than the facts'.
 *
 *  `canToggle` does NOT imply `capability`: an enabled sandbox can always be turned
 *  off (even with the kill switch off — the human exit), but turning ON needs the
 *  capability AND an available host, whatever `canToggle` says. `host: true` marks the
 *  one reason a "Re-check host" can change. */
export function sandboxDisabledReason(
  facts: SandboxFacts, enabled: boolean, busy: boolean,
): { text: string; host: boolean } | null {
  if (busy || facts.busy) return { text: SANDBOX_BUSY, host: false }
  if (enabled) return facts.canToggle ? null : { text: CANT_CHANGE, host: false }
  if (!facts.capability) return { text: SANDBOX_SWITCHED_OFF, host: false }
  if (!facts.available) return { text: facts.unavailableReason || SANDBOX_UNAVAILABLE_FALLBACK, host: true }
  return facts.canToggle ? null : { text: CANT_CHANGE, host: false }
}
