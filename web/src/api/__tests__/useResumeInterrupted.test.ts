import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useResumeInterrupted } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({ api: { post: vi.fn(), get: vi.fn() }, ApiRequestError: class {} }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useResumeInterrupted', () => {
  beforeEach(() => vi.clearAllMocks())

  it('posts the eligible id list to /sessions/batch/resume', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ succeeded: ['s1'], failed: [] })
    const { result } = renderHook(() => useResumeInterrupted(), { wrapper })
    await act(async () => { await result.current.mutateAsync(['s1', 's2']) })
    expect(post).toHaveBeenCalledWith('/sessions/batch/resume', { ids: ['s1', 's2'] })
  })
})
