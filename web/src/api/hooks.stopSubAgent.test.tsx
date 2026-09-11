/**
 * Verifies `useStopSubAgent` posts to the per-agent stop endpoint with the
 * right session/tool-use ids and an empty body.
 *
 * Harness copied from hooks.cancel.test.tsx: a QueryClientProvider wrapper and
 * a mocked `api.post`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
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
  api: {
    post: vi.fn().mockResolvedValue({ ok: true, taskId: 'task1' }),
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

import { api } from '@/api/client'
import { useStopSubAgent } from './hooks'

const post = vi.mocked(api.post)

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  post.mockResolvedValue({ ok: true, taskId: 'task1' })
})

describe('useStopSubAgent', () => {
  it('posts to /sessions/{id}/agents/{toolUseId}/stop', async () => {
    const { result } = renderHook(() => useStopSubAgent(), { wrapper: makeWrapper() })
    await act(async () => {
      await result.current.mutateAsync({ id: 's1', toolUseId: 'toolu_A' })
    })
    expect(post).toHaveBeenCalledWith('/sessions/s1/agents/toolu_A/stop', {})
  })
})
