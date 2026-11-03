/**
 * useRewindSession / useUndoRewind (D-9): one requestId per call, carried in the
 * mutation's variables — a transient failure the mutation retries resends the SAME id
 * (the server then answers with the rewind the first attempt made), while a new call
 * is a new rewind. A refusal is not retried.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import { useRewindSession, useUndoRewind } from '@/api/hooks'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retryDelay: 0 } } })
  return React.createElement(QueryClientProvider, { client: qc }, children)
}

const RESULT = { rewindId: 'r1', prefill: 'ask', filesRestored: ['a.ts'] }
const ids = (post: { mock: { calls: unknown[][] } }) =>
  post.mock.calls.map(([, body]) => (body as { requestId: string }).requestId)

describe('useRewindSession', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('previews with a dry run of the files-and-conversation rewind', async () => {
    const preview = { files: [], deleted: [], insertions: 0, deletions: 0, checkpointAvailable: true }
    const post = vi.spyOn(api, 'post').mockResolvedValue(preview)
    const { result } = renderHook(() => useRewindSession('s/1'), { wrapper })
    let out: unknown
    await act(async () => { out = await result.current.preview('u2') })
    expect(out).toEqual(preview)
    expect(post).toHaveBeenCalledWith('/sessions/s%2F1/rewind', {
      toTurnId: 'u2', mode: 'files_and_conversation', dryRun: true,
      requestId: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4/),
    })
  })

  it('rewinds with the mode given and returns the result', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue(RESULT)
    const { result } = renderHook(() => useRewindSession('s1'), { wrapper })
    let out: unknown
    await act(async () => { out = await result.current.rewind('u2', 'conversation') })
    expect(out).toEqual(RESULT)
    expect(post).toHaveBeenCalledWith('/sessions/s1/rewind', expect.objectContaining({
      toTurnId: 'u2', mode: 'conversation', dryRun: false }))
  })

  it('keeps the requestId across its own retries of a transient failure', async () => {
    const post = vi.spyOn(api, 'post')
      .mockRejectedValueOnce(new ApiRequestError(503, { detail: { code: 'daemon_unavailable' } }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(RESULT)
    const { result } = renderHook(() => useRewindSession('s1'), { wrapper })
    await act(async () => { await result.current.rewind('u2', 'files_and_conversation') })
    expect(post).toHaveBeenCalledTimes(3)
    expect(new Set(ids(post)).size).toBe(1)
  })

  it('does not retry a refusal, and a new call is a new request', async () => {
    const post = vi.spyOn(api, 'post').mockRejectedValue(
      new ApiRequestError(409, { detail: { code: 'REWIND_BUSY', message: 'x' } }))
    const { result } = renderHook(() => useRewindSession('s1'), { wrapper })
    await act(async () => { await result.current.rewind('u2', 'conversation').catch(() => {}) })
    await act(async () => { await result.current.rewind('u2', 'conversation').catch(() => {}) })
    expect(post).toHaveBeenCalledTimes(2)
    const [a, b] = ids(post)
    expect(a).not.toBe(b)
  })
})

describe('useUndoRewind', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('posts the rewindId and returns {ok, skipped}', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, skipped: ['link.txt'] })
    const { result } = renderHook(() => useUndoRewind('s1'), { wrapper })
    let out: unknown
    await act(async () => { out = await result.current.undo('r1') })
    expect(out).toEqual({ ok: true, skipped: ['link.txt'] })
    expect(post).toHaveBeenCalledWith('/sessions/s1/rewind/undo', { rewindId: 'r1' })
  })
})
