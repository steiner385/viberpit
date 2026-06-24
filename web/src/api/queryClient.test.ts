import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Mutation } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import {
  extractMutationErrorMessage,
  handleMutationError,
  MUTATION_ERROR_FALLBACK,
} from './queryClient'

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
