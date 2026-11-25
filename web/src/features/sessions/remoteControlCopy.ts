/**
 * What Remote Control says — THE one table of its copy, shared by the Environment card's
 * row (`rail/RemoteControlControls`) and Settings › Sessions.
 */
import { ApiRequestError } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'

/** The caveat: the Claude apps sit outside ViberPit's own sign-in. */
export const REMOTE_CONTROL_ACCESS_NOTE =
  'Anyone signed in to that Claude account can drive the session from the Claude apps.'

/** Settings › Sessions: on by default, and who can reach a session through it. */
export const REMOTE_CONTROL_DEFAULT_COPY =
  `On by default. Sessions also appear in the Claude web, desktop and mobile apps under the account running them. ${REMOTE_CONTROL_ACCESS_NOTE}`

/** `remoteControlState: "connecting"` — on, but no bridge has answered yet. */
export const REMOTE_CONTROL_CONNECTING = 'Connecting…'
export const REMOTE_CONTROL_CONNECTING_NOTE =
  "Not in the Claude apps yet. It appears there once the session connects; a busy session connects when it's idle."

export const REMOTE_CONTROL_TOGGLE_FALLBACK = "Couldn't change Remote Control"

/** Why an eligible session still can't use it, keyed by `remoteControlUnavailableReason`:
 *  the CLI's own refusal learned at runtime (`login_rejected`, `refused`), or known before
 *  any request (`login_needed`: no usable claude.ai login; `login_busy`: another live session pins
 *  the login; `runner`: a session runner). */
export const REMOTE_CONTROL_UNAVAILABLE_COPY: Record<string, string> = {
  login_needed:
    'Sign in to claude.ai for this account (Accounts → Sign in to claude.ai) to use Remote Control',
  login_busy: "This account's login is in use by another session; try again when it's idle.",
  login_rejected:
    "claude.ai rejected this account's login for Remote Control. Sign in to claude.ai again (Accounts → Sign in to claude.ai).",
  refused: 'The Claude CLI refused Remote Control for this account.',
  runner: 'Remote Control is not available yet for sessions on a session runner.',
}

/** The explanation for *reason*, or a plain line for one this build does not know. */
export function remoteControlUnavailableText(reason: string): string {
  return REMOTE_CONTROL_UNAVAILABLE_COPY[reason] ?? 'Remote Control is unavailable for this account.'
}

/** Whether the row's value is the session's own choice or the Settings default's. */
export function remoteControlSourceLabel(override: boolean | null | undefined): string {
  return typeof override === 'boolean' ? 'Set for this session' : 'Following the default'
}

/** A failed toggle, in plain words: the server's own message (or the session-moving
 *  sentence) when it has one. */
export function remoteControlError(err: unknown): string {
  if (err instanceof ApiRequestError && err.status === 404) return 'This session has already ended.'
  return extractMutationErrorMessage(err, REMOTE_CONTROL_TOGGLE_FALLBACK)
}
