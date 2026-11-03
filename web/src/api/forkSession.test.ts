/**
 * useForkSession (D-8): one requestId per fork() call, carried in the mutation's
 * variables — so a transient failure the mutation retries resends the SAME id
 * (the server then answers with the child the first attempt made), while a new
 * click is a new fork.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import { useForkSession } from '@/api/hooks'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retryDelay: 0 } } })
  return React.createElement(QueryClientProvider, { client: qc }, children)
}

describe('useForkSession', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('POSTs the target with a fresh requestId and returns the child', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ sessionId: 'c1', prefill: null })
    const { result } = renderHook(() => useForkSession('s/1'), { wrapper })
    let out: unknown
    await act(async () => { out = await result.current.fork({ atTurnId: 't1', mode: 'reply' }) })
    expect(out).toEqual({ sessionId: 'c1', prefill: null })
    expect(post).toHaveBeenCalledWith('/sessions/s%2F1/fork', {
      atTurnId: 't1', mode: 'reply', requestId: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4/),
    })
  })

  it('keeps the requestId across its own retries of a transient failure', async () => {
    const post = vi.spyOn(api, 'post')
      .mockRejectedValueOnce(new ApiRequestError(503, { detail: { code: 'daemon_unavailable' } }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue({ sessionId: 'c1', prefill: 'hi' })
    const { result } = renderHook(() => useForkSession('s1'), { wrapper })
    await act(async () => { await result.current.fork({ atTurnId: null, mode: 'message', prefill: 'hi' }) })
    expect(post).toHaveBeenCalledTimes(3)
    const ids = post.mock.calls.map(([, body]) => (body as { requestId: string }).requestId)
    expect(new Set(ids).size).toBe(1)
  })

  it('does not retry a refusal, and a new call is a new request', async () => {
    const post = vi.spyOn(api, 'post').mockRejectedValue(
      new ApiRequestError(409, { detail: { code: 'TURN_NOT_FORKABLE', message: 'x' } }))
    const { result } = renderHook(() => useForkSession('s1'), { wrapper })
    await act(async () => { await result.current.fork({ atTurnId: 't1', mode: 'reply' }).catch(() => {}) })
    await act(async () => { await result.current.fork({ atTurnId: 't1', mode: 'reply' }).catch(() => {}) })
    expect(post).toHaveBeenCalledTimes(2)
    const [a, b] = post.mock.calls.map(([, body]) => (body as { requestId: string }).requestId)
    expect(a).not.toBe(b)
  })
})
