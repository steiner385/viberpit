import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useStageModels } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useStageModels', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetches /pipeline/repos and returns a repo→model map', async () => {
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      'o/r': {
        repo: 'o/r',
        nodes: [{ id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' }],
      },
    })
    const { result } = renderHook(() => useStageModels(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/pipeline/repos')
    expect(result.current.data!['o/r'].nodes[0].id).toBe('ci')
  })
})
