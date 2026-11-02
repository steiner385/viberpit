/**
 * `useSessionDiff` (D-4): the `GET /sessions/{id}/diff` query behind the
 * header's `+N −M` chip and the Changes tab.
 *
 * - the flags map onto `summary=1` / `ignoreWhitespace=1`;
 * - nothing polls: a settled summary is not fetched again on its own;
 * - a turn ending (`busy` true → false) refetches it — never a session switch;
 * - focusing the window does not refetch it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
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

describe('useSessionDiff — what is not a turn end', () => {
  it('does not read a session switch as the new session\'s turn ending', async () => {
    // Cached data never goes stale here, so only an invalidation could fetch again.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    const { result, rerender } = renderHook(
      ({ id, busy }: { id: string; busy: boolean }) => useSessionDiff(id, { summary: true, busy }),
      { wrapper, initialProps: { id: 's2', busy: false } },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    rerender({ id: 's1', busy: true })
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    // Back to s2, idle: s1's running turn did not end in s2.
    rerender({ id: 's2', busy: false })
    await new Promise((r) => setTimeout(r, 20))
    expect(vi.mocked(api.get).mock.calls.map((c) => c[0])).toEqual([
      '/sessions/s2/diff?summary=1', '/sessions/s1/diff?summary=1',
    ])
  })

  it('does not refetch when the window regains focus', async () => {
    const { result } = renderHook(() => useSessionDiff('s1', { summary: true }), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    try {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
      await new Promise((r) => setTimeout(r, 20))
      expect(api.get).toHaveBeenCalledTimes(1)
    } finally {
      focusManager.setFocused(undefined)
    }
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
