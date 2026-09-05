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
export const ENGINE_UNAVAILABLE_FALLBACK =
  'No runner can start the selected agent — check Settings → Fleet.'
export const MODEL_EFFORT_MISMATCH_FALLBACK =
  'That model and reasoning effort are not offered together — pick another.'
export const MODELS_UNAVAILABLE_MESSAGE =
  'Could not load the model list for this account — its app-server did not answer.'

export type CreateSessionFailure =
  /** The user picked a branch the server could not switch to. Recoverable in place. */
  | { kind: 'branch'; message: string }
  /** Every account needs a re-login. Only Settings → Accounts fixes it. */
  | { kind: 'reauth'; message: string }
  /** Every account is at its usage ceiling. The shell's banner owns this one. */
  | { kind: 'all-limited'; earliestReset: string | null }
  /**
   * No runner can run the requested ENGINE — a missing Codex binary, a drifted
   * app-server contract, external auth off, or the engine simply not installed.
   * Distinct from all-limited on purpose: usage is not the problem, and pointing
   * the operator at account quotas is the wrong recovery. `hostReasons` names
   * which runner to fix (empty when the server did not say).
   */
  | { kind: 'engine'; message: string; hostReasons: Record<string, string> }
  /** The model/effort pair the account's catalog rejected. */
  | { kind: 'model'; message: string; model: string | null; effort: string | null }
  /** Anything else: the body's message if it has one, a fixed line if not. */
  | { kind: 'other'; message: string }

/**
 * The error body, normalized across the three envelopes the API actually uses.
 *
 * `{"error": {"code", "message"}}` is the create endpoint's own shape;
 * `{"error": {"error": …}}` is a domain wire dict nested under it (the Codex
 * catalog's `model_effort_mismatch`); `{"detail": {"error", "message"}}` is what
 * FastAPI's `HTTPException(detail={...})` produces. Reading only the first one
 * is how a Codex failure would arrive as an unexplained generic line.
 *
 * Narrowed, not asserted: an error body is genuinely arbitrary JSON (R10 — no
 * `as unknown as T`).
 */
function readErrorBody(body: unknown): {
  code?: string
  message?: string
  model: string | null
  effort: string | null
  hostReasons: Record<string, string>
  earliestReset: string | null
} {
  if (typeof body !== 'object' || body === null) {
    return { model: null, effort: null, hostReasons: {}, earliestReset: null }
  }
  const record: Record<string, unknown> = { ...body }
  const nested = (key: string): Record<string, unknown> =>
    typeof record[key] === 'object' && record[key] !== null
      ? (record[key] as Record<string, unknown>)
      : {}
  const error = nested('error')
  const detail = nested('detail')
  const pick = (key: string): string | undefined => {
    for (const source of [error, detail]) {
      const value = source[key]
      if (typeof value === 'string') return value
    }
    return undefined
  }
  const rawReasons = error.hostReasons ?? detail.hostReasons
  const hostReasons: Record<string, string> = {}
  if (typeof rawReasons === 'object' && rawReasons !== null) {
    for (const [host, why] of Object.entries(rawReasons as Record<string, unknown>)) {
      if (typeof why === 'string') hostReasons[host] = why
    }
  }
  return {
    // `code` is the create endpoint's spelling; `error` the domain dicts'. One
    // field here so every branch below asks the same question once.
    code: pick('code') ?? pick('error'),
    message: pick('message'),
    model: pick('model') ?? null,
    effort: pick('effort') ?? null,
    hostReasons,
    earliestReset: typeof record.earliestReset === 'string' ? record.earliestReset : null,
  }
}

export function describeCreateSessionFailure(err: unknown): CreateSessionFailure {
  if (err instanceof ApiRequestError) {
    const { code, message, model, effort, hostReasons, earliestReset } = readErrorBody(err.body)

    if (code === 'codex_models_unavailable') {
      return { kind: 'engine', message: MODELS_UNAVAILABLE_MESSAGE, hostReasons }
    }
    // `no_engine_host_available` is a 409 like the usage ceiling, and BEFORE the
    // status check on purpose: `NoEngineHostAvailable` subclasses
    // `NoHostAvailable`, so without its own branch "install the Codex binary"
    // falls through to the all-limited banner and sends the operator to look at
    // account quotas. `engine_unavailable` is the in-process twin (no fleet).
    if (code === 'no_engine_host_available' || code === 'engine_unavailable') {
      return { kind: 'engine', message: message ?? ENGINE_UNAVAILABLE_FALLBACK, hostReasons }
    }
    if (code === 'model_effort_mismatch') {
      return { kind: 'model', message: MODEL_EFFORT_MISMATCH_FALLBACK, model, effort }
    }

    if (err.status === 409) {
      if (code === 'branch_switch_failed') {
        return { kind: 'branch', message: message ?? 'Could not switch branch' }
      }
      if (code === 'accounts_need_reauth') return { kind: 'reauth', message: REAUTH_MESSAGE }
      return { kind: 'all-limited', earliestReset }
    }
  }
  return { kind: 'other', message: extractMutationErrorMessage(err, CREATE_SESSION_FALLBACK) }
}
