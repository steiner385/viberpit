/**
 * queryClient — the app's single TanStack Query client, wired with a global
 * MutationCache.onError so any failed mutation surfaces a sonner error toast.
 *
 * A mutation can opt out of the global toast (e.g. when it renders its own inline
 * error) by setting `meta: { suppressErrorToast: true }`. No mutation does so today;
 * it is a documented extension point.
 */
import { QueryClient, MutationCache, type Mutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import { clientLogger } from '@/obs/logger'

export const MUTATION_ERROR_FALLBACK = 'Something went wrong. Please try again.'

/** Derive a human-readable message from a mutation error. */
export function extractMutationErrorMessage(error: unknown): string {
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
  return MUTATION_ERROR_FALLBACK
}

/** Global MutationCache.onError handler: log to clientLogger, then toast unless the mutation opts out. */
export function handleMutationError(
  error: unknown,
  mutation: Mutation<unknown, unknown, unknown>,
): void {
  const message = extractMutationErrorMessage(error)
  clientLogger.error(`mutation: ${message}`, {
    stack: error instanceof Error ? (error.stack ?? String(error)) : String(error),
  })
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
