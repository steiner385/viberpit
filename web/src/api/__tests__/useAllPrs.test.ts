import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useAllPrs } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useAllPrs', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetches the PR population from /prs', async () => {
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      { number: 1, url: 'u', state: 'open', checks: 'failing', title: 't',
        deployed: false, repo: 'o/n', stage: 'parked', session: { id: 's1', name: 'Fix' } },
    ])
    const { result } = renderHook(() => useAllPrs(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/prs')
    expect(result.current.data?.[0].session?.id).toBe('s1')
  })
})
