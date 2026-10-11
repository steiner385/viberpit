import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { api } from './client'
import { useUpdatePermissionRule, useRevokePermissionRule } from './hooks'

// Callers render these errors inline, so the global toast must stay quiet.
describe('permission-rule mutations', () => {
  const setup = () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)
    return { qc, wrapper }
  }

  it('update suppresses the global error toast', async () => {
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('x'))
    const { qc, wrapper } = setup()
    const { result } = renderHook(() => useUpdatePermissionRule(), { wrapper })
    await act(async () => { await result.current.mutateAsync({ id: 'r1', pattern: 'p' }).catch(() => {}) })
    expect(qc.getMutationCache().getAll()[0].options.meta?.suppressErrorToast).toBe(true)
  })

  it('revoke suppresses the global error toast', async () => {
    vi.spyOn(api, 'del').mockRejectedValue(new Error('x'))
    const { qc, wrapper } = setup()
    const { result } = renderHook(() => useRevokePermissionRule(), { wrapper })
    await act(async () => { await result.current.mutateAsync('r1').catch(() => {}) })
    expect(qc.getMutationCache().getAll()[0].options.meta?.suppressErrorToast).toBe(true)
  })
})
