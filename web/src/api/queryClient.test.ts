// @vitest-environment node
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Mutation } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import {
  extractMutationErrorMessage,
  handleMutationError,
  shouldRetryQuery,
  queryClient,
  MUTATION_ERROR_FALLBACK,
  TRANSIENT_RETRY_LIMIT,
} from './queryClient'
import { clientLogger } from '@/obs/logger'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))
import { toast } from 'sonner'

// Minimal stand-in for a TanStack Mutation — handleMutationError only reads options.meta.
function fakeMutation(meta?: Record<string, unknown>) {
  return { options: { meta } } as unknown as Mutation<unknown, unknown, unknown>
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('extractMutationErrorMessage', () => {
  it('uses body.detail.message when present', () => {
    const err = new ApiRequestError(409, { detail: { code: 'resume_failed', message: 'Session could not be resumed.' } })
    expect(extractMutationErrorMessage(err)).toBe('Session could not be resumed.')
  })

  it('falls back to body.message when there is no detail.message', () => {
    const err = new ApiRequestError(400, { message: 'Bad request' })
    expect(extractMutationErrorMessage(err)).toBe('Bad request')
  })

  it('returns the fallback when an ApiRequestError carries no usable message', () => {
    const err = new ApiRequestError(500, {})
    expect(extractMutationErrorMessage(err)).toBe(MUTATION_ERROR_FALLBACK)
  })

  it('returns the fallback for a non-API error', () => {
    expect(extractMutationErrorMessage(new Error('network boom'))).toBe(MUTATION_ERROR_FALLBACK)
  })
})

describe('handleMutationError', () => {
  it('shows an error toast with the extracted message', () => {
    const err = new ApiRequestError(404, { detail: { code: 'not_found', message: 'Session not found' } })
    handleMutationError(err, fakeMutation())
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(toast.error).toHaveBeenCalledWith('Session not found')
  })

  it('does not toast when the mutation opts out via meta.suppressErrorToast', () => {
    const err = new ApiRequestError(404, { detail: { code: 'not_found', message: 'Session not found' } })
    handleMutationError(err, fakeMutation({ suppressErrorToast: true }))
    expect(toast.error).not.toHaveBeenCalled()
  })
})

describe('shouldRetryQuery', () => {
  it.each([502, 503, 504])('retries a transient %i up to the limit', (status) => {
    const err = new ApiRequestError(status, { detail: 'session daemon unavailable' })
    expect(shouldRetryQuery(0, err)).toBe(true)
    expect(shouldRetryQuery(TRANSIENT_RETRY_LIMIT - 1, err)).toBe(true)
    expect(shouldRetryQuery(TRANSIENT_RETRY_LIMIT, err)).toBe(false)
  })

  it('retries network-level failures (fetch rejects before any HTTP status)', () => {
    expect(shouldRetryQuery(0, new TypeError('Failed to fetch'))).toBe(true)
    expect(shouldRetryQuery(TRANSIENT_RETRY_LIMIT, new TypeError('Failed to fetch'))).toBe(false)
  })

  it.each([400, 401, 403, 404, 409, 422])('never retries a deterministic %i', (status) => {
    expect(shouldRetryQuery(0, new ApiRequestError(status, {}))).toBe(false)
  })

  it('never retries a 500 — internal errors are bugs, not blips', () => {
    expect(shouldRetryQuery(0, new ApiRequestError(500, {}))).toBe(false)
  })
})

describe('queryClient defaults', () => {
  it('wires shouldRetryQuery as the default query retry policy', () => {
    expect(queryClient.getDefaultOptions().queries?.retry).toBe(shouldRetryQuery)
  })
})

describe('handleMutationError forwarding', () => {
  it('forwards the error to the client logger', () => {
    const spy = vi.spyOn(clientLogger, 'error')
    const err = new ApiRequestError(500, { detail: { message: 'server fail' } })
    handleMutationError(err, { options: {} } as never)
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('server fail'), expect.objectContaining({ stack: expect.any(String) }))
  })
})
