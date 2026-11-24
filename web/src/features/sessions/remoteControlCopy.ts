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

export const REMOTE_CONTROL_TOGGLE_FALLBACK = "Couldn't change Remote Control"

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
