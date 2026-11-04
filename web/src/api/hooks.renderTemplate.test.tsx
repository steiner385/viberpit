import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { api } from './client'
import { useRenderTemplate } from './hooks'

// The dispatch bar says a failed render inline, so the global toast must stay quiet.
describe('useRenderTemplate', () => {
  it('suppresses the global error toast', async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new Error('x'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)
    const { result } = renderHook(() => useRenderTemplate(), { wrapper })
    await act(async () => { await result.current.mutateAsync({ id: 't1', variables: {} }).catch(() => {}) })
    expect(qc.getMutationCache().getAll()[0].options.meta?.suppressErrorToast).toBe(true)
  })
})
