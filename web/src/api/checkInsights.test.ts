import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useCheckInsights } from './hooks'
import { api } from './client'

vi.mock('./client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
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
}))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return React.createElement(QueryClientProvider, { client: qc }, children)
}

describe('useCheckInsights', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetches /pipeline/insights/checks with repo + window', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      repo: 'o/n', window: '7d', slowest: [], flakiest: [],
    })
    const { result } = renderHook(() => useCheckInsights('o/n', '7d'), { wrapper })
    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(get).toHaveBeenCalledWith('/pipeline/insights/checks?repo=o%2Fn&window=7d')
    expect(result.current.data!.repo).toBe('o/n')
    expect(result.current.data!.slowest).toEqual([])
    expect(result.current.data!.flakiest).toEqual([])
  })

  it('is disabled when repo is undefined', () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ repo: '', window: '7d', slowest: [], flakiest: [] })
    const { result } = renderHook(() => useCheckInsights(undefined, '7d'), { wrapper })
    expect(result.current.fetchStatus).toBe('idle')
    expect(get).not.toHaveBeenCalled()
  })
})
