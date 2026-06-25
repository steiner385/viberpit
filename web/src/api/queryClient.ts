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

export const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    // v5 onError signature: (error, variables, onMutateResult, mutation, context)
    onError: (error, _variables, _onMutateResult, mutation) => handleMutationError(error, mutation),
  }),
})
