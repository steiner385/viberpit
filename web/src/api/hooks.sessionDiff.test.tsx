/**
 * `useSessionDiff` (D-4): the `GET /sessions/{id}/diff` query behind the
 * header's `+N −M` chip and the Changes tab.
 *
 * - the flags map onto `summary=1` / `ignoreWhitespace=1`;
 * - nothing polls: a settled summary is not fetched again on its own;
 * - a turn ending (`busy` true → false) refetches it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useSessionDiff } from './hooks'

vi.mock('./client', () => ({
  ApiRequestError: class ApiRequestError extends Error {},
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from './client'

const EMPTY = { base: null, head: null, files: [], additions: 0, deletions: 0, truncated: false }

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue(EMPTY)
})

describe('useSessionDiff', () => {
  it('requests the summary with whitespace ignored when asked', async () => {
    const { result } = renderHook(
      () => useSessionDiff('s 1', { summary: true, ignoreWhitespace: true }),
      { wrapper: makeWrapper() },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/sessions/s%201/diff?summary=1&ignoreWhitespace=1')
  })

  it('requests the full diff with no flags', async () => {
    const { result } = renderHook(() => useSessionDiff('s1'), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/sessions/s1/diff')
  })

  it('refetches when a turn ends (busy → idle), and not otherwise', async () => {
    const { result, rerender } = renderHook(
      ({ busy }: { busy: boolean }) => useSessionDiff('s1', { summary: true, busy }),
      { wrapper: makeWrapper(), initialProps: { busy: false } },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledTimes(1)

    rerender({ busy: false })
    rerender({ busy: true })
    // Starting a turn is not a reason to fetch: the worktree is mid-change.
    expect(api.get).toHaveBeenCalledTimes(1)

    rerender({ busy: false })
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  })
})

describe('useSessionDiff — placeholder across a flag flip', () => {
  it("keeps the same session's diff while the whitespace variant loads, but not another session's", async () => {
    const ONE = { ...EMPTY, additions: 1, files: [] }
    vi.mocked(api.get).mockResolvedValueOnce(ONE).mockReturnValue(new Promise(() => {}))
    const { result, rerender } = renderHook(
      ({ id, ws }: { id: string; ws: boolean }) => useSessionDiff(id, { ignoreWhitespace: ws }),
      { wrapper: makeWrapper(), initialProps: { id: 's1', ws: false } },
    )
    await waitFor(() => expect(result.current.data).toEqual(ONE))
    rerender({ id: 's1', ws: true })
    expect(result.current.data).toEqual(ONE)
    rerender({ id: 's2', ws: true })
    expect(result.current.data).toBeUndefined()
  })
})
