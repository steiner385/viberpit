/**
 * `useSentFiles` (#3487): the `POST /sessions/{id}/sent-files/{toolUseId}` publish
 * behind the SendUserFile card — one request per tool call, cached like a read,
 * never sent without a session or while disabled, never retried.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useSentFiles } from './sentFilesHooks'

vi.mock('./client', () => ({
  ApiRequestError: class ApiRequestError extends Error {},
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from './client'

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

  it('does not retry a refusal', async () => {
    vi.mocked(api.post).mockRejectedValue(new Error('404'))
    const qc = new QueryClient()  // the app default would retry; the hook must not
    const { result } = renderHook(() => useSentFiles('s1', 'toolu_a'), { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(api.post).toHaveBeenCalledTimes(1)
  })
})
