/**
 * Verifies that the two fire-and-forget cancel mutations (useCancelReauth,
 * useCancelAddAccount) opt out of the global MutationCache.onError toast
 * via meta: { suppressErrorToast: true }.
 *
 * Strategy: build a fresh QueryClient wired identically to production
 * (MutationCache + handleMutationError), mock the api.post to reject, and
 * assert toast.error is NOT called for the cancel hooks — while a control
 * mutation WITHOUT the meta WOULD call it, proving the wiring works.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, MutationCache, useMutation } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import { handleMutationError } from '@/api/queryClient'
import { useCancelReauth, useCancelAddAccount } from './hooks'

// Mock sonner so toast.error is a spy we can assert on.
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))
import { toast } from 'sonner'

// Mock the api object so api.post rejects with an ApiRequestError.
vi.mock('@/api/client', () => ({
  ApiRequestError: class ApiRequestError extends Error {
    readonly status: number
    readonly body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.name = 'ApiRequestError'
      this.status = status
      this.body = body
    }
  },
  api: {
    post: vi.fn().mockRejectedValue(
      new (class ApiRequestError extends Error {
        readonly status = 500
        readonly body = { message: 'server error' }
        constructor() { super('API error 500'); this.name = 'ApiRequestError' }
      })()
    ),
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

/** Build a fresh QueryClient wired the same way as production. */
function makeClient() {
  return new QueryClient({
    defaultOptions: { mutations: { retry: false } },
    mutationCache: new MutationCache({
      onError: (error, _variables, _onMutateResult, mutation) =>
        handleMutationError(error, mutation),
    }),
  })
}

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useCancelReauth — fire-and-forget cancel', () => {
  it('does NOT toast when the mutation rejects (suppressErrorToast meta)', async () => {
    const client = makeClient()
    const wrapper = makeWrapper(client)
    const { result } = renderHook(() => useCancelReauth(), { wrapper })

    result.current.mutate('work')
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(toast.error).not.toHaveBeenCalled()
  })
})

describe('useCancelAddAccount — fire-and-forget cancel', () => {
  it('does NOT toast when the mutation rejects (suppressErrorToast meta)', async () => {
    const client = makeClient()
    const wrapper = makeWrapper(client)
    const { result } = renderHook(() => useCancelAddAccount(), { wrapper })

    result.current.mutate()
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(toast.error).not.toHaveBeenCalled()
  })
})

describe('positive control — mutation WITHOUT suppressErrorToast meta', () => {
  it('DOES toast when a normal mutation rejects (proving the wiring works)', async () => {
    const client = makeClient()
    const wrapper = makeWrapper(client)

    // Inline hook that has no meta — should trigger the global toast.
    const { result } = renderHook(
      () =>
        useMutation<void, ApiRequestError, void>({
          mutationFn: () =>
            Promise.reject(new ApiRequestError(500, { message: 'server error' })),
          // No meta: no suppressErrorToast — global handler should fire.
        }),
      { wrapper },
    )

    result.current.mutate()
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(toast.error).toHaveBeenCalledTimes(1)
  })
})
