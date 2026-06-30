import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useMergeTrains } from './hooks'
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

describe('useMergeTrains', () => {
  it('fetches the per-repo train map from /pipeline/trains', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      'cairnea/KinDash': { repo: 'cairnea/KinDash', depth: 5, ejects_24h: 46 },
    })
    const { result } = renderHook(() => useMergeTrains(), { wrapper })
    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(get).toHaveBeenCalledWith('/pipeline/trains')
    expect(result.current.data!['cairnea/KinDash'].depth).toBe(5)
  })
})
