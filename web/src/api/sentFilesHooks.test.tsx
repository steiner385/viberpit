/**
 * `useSentFiles` (#3487): the `POST /sessions/{id}/sent-files/{toolUseId}` publish
 * behind the SendUserFile card — one request per tool call, cached like a read,
 * never sent without a session or while disabled; a 503 (history not readable in
 * time) or 404 (the call not on disk yet) is retried a bounded number of times.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { shouldRetrySentFiles, useSentFiles } from './sentFilesHooks'

vi.mock('./client', () => ({
  ApiRequestError: class ApiRequestError extends Error {},
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from './client'

const refusal = (status: number) => Object.assign(new Error(`API error ${status}`), { status })

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })

function wrapperFor(qc: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.post).mockResolvedValue({ files: [] })
})

describe('useSentFiles', () => {
  it('posts once per tool call, keyed by session and tool id', async () => {
    const wrapper = wrapperFor(client())
    const a = renderHook(() => useSentFiles('s 1', 'toolu_a'), { wrapper })
    const again = renderHook(() => useSentFiles('s 1', 'toolu_a'), { wrapper })
    const b = renderHook(() => useSentFiles('s 1', 'toolu_b'), { wrapper })
    await waitFor(() => expect(a.result.current.isSuccess && again.result.current.isSuccess && b.result.current.isSuccess).toBe(true))
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ['/sessions/s%201/sent-files/toolu_a'],
      ['/sessions/s%201/sent-files/toolu_b'],
    ])
  })

  it('sends nothing without a session id or while disabled', async () => {
    const wrapper = wrapperFor(client())
    const none = renderHook(() => useSentFiles(null, 'toolu_a'), { wrapper })
    const off = renderHook(() => useSentFiles('s1', 'toolu_a', { enabled: false }), { wrapper })
    await new Promise((r) => setTimeout(r, 0))
    expect(none.result.current.fetchStatus).toBe('idle')
    expect(off.result.current.fetchStatus).toBe('idle')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('retries unavailable history (503) with backoff until it succeeds', async () => {
    vi.mocked(api.post).mockRejectedValueOnce(refusal(503)).mockResolvedValueOnce({ files: [{ path: '/tmp/a.png', url: '/api/attachments/a', mediaType: 'image/png', reason: null }] })
    const { result } = renderHook(() => useSentFiles('s1', 'toolu_a'), { wrapper: wrapperFor(new QueryClient()) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true), { timeout: 4000 })
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(result.current.data?.files[0].url).toBe('/api/attachments/a')
  })

  it('recovers from a racy first 404 without a reload', async () => {
    vi.mocked(api.post).mockRejectedValueOnce(refusal(404)).mockResolvedValueOnce({ files: [] })
    const { result } = renderHook(() => useSentFiles('s1', 'toolu_a'), { wrapper: wrapperFor(new QueryClient()) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true), { timeout: 4000 })
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it('does not retry a refusal a later read cannot change', async () => {
    vi.mocked(api.post).mockRejectedValue(refusal(409))
    const { result } = renderHook(() => useSentFiles('s1', 'toolu_a'), { wrapper: wrapperFor(new QueryClient()) })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(api.post).toHaveBeenCalledTimes(1)
  })
})

describe('shouldRetrySentFiles', () => {
  it('bounds retries per status', () => {
    expect([0, 1, 2, 3, 4].map((n) => shouldRetrySentFiles(n, refusal(503)))).toEqual([true, true, true, true, false])
    expect([0, 1, 2].map((n) => shouldRetrySentFiles(n, refusal(404)))).toEqual([true, true, false])
    expect(shouldRetrySentFiles(0, refusal(500))).toBe(false)
    expect(shouldRetrySentFiles(0, new Error('network'))).toBe(false)
  })
})
