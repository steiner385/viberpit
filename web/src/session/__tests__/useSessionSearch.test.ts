import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useSessionSearch } from '../useSessionSearch'
import { api } from '@/api/client'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useSessionSearch', () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('returns null matchedIds and never fetches for an empty query', () => {
    const spy = vi.spyOn(api, 'get')
    const { result } = renderHook(() => useSessionSearch(), { wrapper })
    expect(result.current.matchedIds).toBeNull()
    expect(result.current.isFiltered).toBe(false)
    expect(spy).not.toHaveBeenCalled()
  })

  it('debounces, fetches, and exposes matched ids as a Set', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValue({ ids: ['s1', 's2'] })
    const { result } = renderHook(() => useSessionSearch(), { wrapper })

    act(() => result.current.setQuery('auth'))
    // Before the debounce window elapses, no request.
    expect(spy).not.toHaveBeenCalled()

    await act(async () => { vi.advanceTimersByTime(300) })
    await waitFor(() => expect(result.current.matchedIds).not.toBeNull())

    expect(spy).toHaveBeenCalledWith('/sessions/search?q=auth&scope=live')
    expect([...(result.current.matchedIds ?? [])].sort()).toEqual(['s1', 's2'])
    expect(result.current.isFiltered).toBe(true)
  })

  it('targets the archived scope when asked', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValue({ ids: ['a1', 'a2'] })
    const { result } = renderHook(() => useSessionSearch('archived'), { wrapper })

    act(() => result.current.setQuery('auth'))
    expect(spy).not.toHaveBeenCalled()

    await act(async () => { vi.advanceTimersByTime(300) })
    await waitFor(() => expect(result.current.matchedIds).not.toBeNull())

    expect(spy).toHaveBeenCalledWith('/sessions/search?q=auth&scope=archived')
    expect([...(result.current.matchedIds ?? [])].sort()).toEqual(['a1', 'a2'])
    expect(result.current.isFiltered).toBe(true)
  })

  it('falls back to null matchedIds and isFiltered=false when the request rejects', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useSessionSearch(), { wrapper })

    act(() => result.current.setQuery('auth'))
    await act(async () => { vi.advanceTimersByTime(300) })
    await waitFor(() => expect(result.current.matchedIds).toBeNull())

    expect(result.current.matchedIds).toBeNull()
    expect(result.current.isFiltered).toBe(false)
  })
})
