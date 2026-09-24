/**
 * queryClient — the app's single TanStack Query client, wired with a global
 * MutationCache.onError so any failed mutation surfaces a sonner error toast.
 *
 * A mutation can opt out of the global toast by setting
 * `meta: { suppressErrorToast: true }`. It should do so whenever its caller renders
 * the failure inline (a dialog, a form, a panel that stays on screen) — the toast
 * exists for failures that have no visible owner, not as a second copy of one.
 */
import { QueryClient, MutationCache, type Mutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import { clientLogger } from '@/obs/logger'

export const MUTATION_ERROR_FALLBACK = 'Something went wrong. Please try again.'

/** Derive a human-readable message from a mutation error. Unwraps the server's
 *  `{detail: {message}}`, `{detail: "…"}` and `{message}` body shapes; `fallback`
 *  lets an inline caller say what failed ("Could not advance initiative") when
 *  the body carries nothing usable. */
export function extractMutationErrorMessage(
  error: unknown,
  fallback: string = MUTATION_ERROR_FALLBACK,
): string {
  if (error instanceof ApiRequestError) {
    const detail = error.body?.detail
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const msg = (detail as Record<string, unknown>).message
      if (typeof msg === 'string' && msg.trim() !== '') return msg
    }
    if (typeof detail === 'string' && detail.trim() !== '') return detail
    const top = error.body?.message
    if (typeof top === 'string' && top.trim() !== '') return top
  }
  return fallback
}

/**
 * A 4xx is the server deliberately refusing — every account over its usage ceiling,
 * a double-accepted proposal, a tier-gated model. The user already sees it as a toast
 * or inline message, and the client is not at fault, so it logs at `info`: kept for
 * debugging, below the ingest floor, never filed as an auto-error issue. Anything else
 * (5xx, a network failure, a non-`ApiRequestError` throw) is a real defect and stays at
 * `error`. Server-side state behind a refusal belongs in *server* logs at the point of
 * refusal, not in the client's echo of the response.
 */
function isDeliberateRefusal(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status >= 400 && error.status < 500
}

/** Global MutationCache.onError handler: log to clientLogger, then toast unless the mutation opts out. */
export function handleMutationError(
  error: unknown,
  mutation: Mutation<unknown, unknown, unknown>,
): void {
  const message = extractMutationErrorMessage(error)
  const extra = { stack: error instanceof Error ? (error.stack ?? String(error)) : String(error) }
  if (isDeliberateRefusal(error)) clientLogger.info(`mutation: ${message}`, extra)
  else clientLogger.error(`mutation: ${message}`, extra)
  if (mutation.options.meta?.suppressErrorToast) return
  toast.error(message)
}

/**
 * Query retries beyond this count stop; with react-query's default exponential
 * backoff (1s, 2s, 4s, 8s, 16s) five retries rides out a ~30s outage — comfortably
 * covering the few-second window where a deploy restart answers 503.
 */
export const TRANSIENT_RETRY_LIMIT = 5

/**
 * Default query retry policy. The server maps an unreachable session daemon to
 * 503 (see app.py's DaemonUnavailable handler) as an explicit "try again" signal,
 * so transient statuses and network-level failures get a generous retry budget,
 * while deterministic errors (4xx, 500) fail fast — retrying a 401 or 404 can
 * never succeed. Per-query `retry` options still override this default.
 */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiRequestError && ![502, 503, 504].includes(error.status)) {
    return false
  }
  return failureCount < TRANSIENT_RETRY_LIMIT
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: shouldRetryQuery },
  },
  mutationCache: new MutationCache({
    // v5 onError signature: (error, variables, onMutateResult, mutation, context)
    onError: (error, _variables, _onMutateResult, mutation) => handleMutationError(error, mutation),
  }),
})
