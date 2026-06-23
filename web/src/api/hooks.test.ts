import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useSendPrompt } from './hooks'
import { api } from './client'
import type { Session } from './types'

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

describe('useSendPrompt', () => {
  it('useSendPrompt POSTs to /sessions/{id}/prompt', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({} as Session)
    const { result } = renderHook(() => useSendPrompt(), { wrapper })
    result.current.mutate({ id: 's1', text: 'continue' })
    await waitFor(() => expect(post).toHaveBeenCalledWith('/sessions/s1/prompt', { text: 'continue' }))
  })
})
