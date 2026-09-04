/**
 * The one reading of a failed `POST /sessions`.
 *
 * Both places that create a session — the New Session dialog and the dispatch
 * bar in the dock — used to carry their own copy of the 409 triage, and the two
 * had already drifted (one narrowed the body, the other cast it). They also
 * both toasted the "no usable accounts" line, on top of the global mutation
 * toast, so a single failure could show three messages that covered the page
 * header on a phone.
 *
 * `useCreateSession` now opts out of the global toast; every caller renders
 * the failure inline next to the form that raised it, and this is the shape it
 * renders from.
 */
import { ApiRequestError } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'

export const REAUTH_MESSAGE = 'No usable accounts — check Settings → Accounts to re-authenticate.'
export const CREATE_SESSION_FALLBACK = 'Could not start the session.'

export type CreateSessionFailure =
  /** The user picked a branch the server could not switch to. Recoverable in place. */
  | { kind: 'branch'; message: string }
  /** Every account needs a re-login. Only Settings → Accounts fixes it. */
  | { kind: 'reauth'; message: string }
  /** Every account is at its usage ceiling. The shell's banner owns this one. */
  | { kind: 'all-limited'; earliestReset: string | null }
  /** Anything else: the body's message if it has one, a fixed line if not. */
  | { kind: 'other'; message: string }

/** The 409 body shape the create endpoint returns. Narrowed, not asserted:
 *  an error body is genuinely arbitrary JSON (R10 — no `as unknown as T`). */
function read409(body: unknown): { code?: string; message?: string; earliestReset: string | null } {
  if (typeof body !== 'object' || body === null) return { earliestReset: null }
  const record: Record<string, unknown> = { ...body }
  const error = typeof record.error === 'object' && record.error !== null
    ? (record.error as Record<string, unknown>)
    : {}
  return {
    code: typeof error.code === 'string' ? error.code : undefined,
    message: typeof error.message === 'string' ? error.message : undefined,
    earliestReset: typeof record.earliestReset === 'string' ? record.earliestReset : null,
  }
}

export function describeCreateSessionFailure(err: unknown): CreateSessionFailure {
  if (err instanceof ApiRequestError && err.status === 409) {
    const { code, message, earliestReset } = read409(err.body)
    if (code === 'branch_switch_failed') {
      return { kind: 'branch', message: message ?? 'Could not switch branch' }
    }
    if (code === 'accounts_need_reauth') return { kind: 'reauth', message: REAUTH_MESSAGE }
    return { kind: 'all-limited', earliestReset }
  }
  return { kind: 'other', message: extractMutationErrorMessage(err, CREATE_SESSION_FALLBACK) }
}
