/**
 * D-15: the PR tab's data hooks — the detail read and the two mutations — hit the
 * routes Task 1 serves with the bodies it accepts, and invalidate the detail and the
 * sessions list on success.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { QK, useMergePr, usePrDetail, useSetAutoMerge } from './hooks'
import { api } from './client'

vi.mock('./client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
  apiErrorCode: () => null,
  ApiRequestError: class ApiRequestError extends Error {
    readonly status: number
    readonly body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
}))

let qc: QueryClient
function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: qc }, children)
}

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

describe('PR tab hooks (D-15)', () => {
  it('usePrDetail GETs /prs/{owner}/{name}/{number}', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ number: 7 })
    const { result } = renderHook(() => usePrDetail('acme/widgets', 7), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(get).toHaveBeenCalledWith('/prs/acme/widgets/7')
  })

  it('usePrDetail stays off without a repo', () => {
    const get = vi.spyOn(api, 'get')
    renderHook(() => usePrDetail(null, 7), { wrapper })
    expect(get).not.toHaveBeenCalled()
  })

  it('useMergePr POSTs {method: "squash"} and invalidates detail + sessions', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, error: '' })
    const inval = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMergePr(), { wrapper })
    await result.current.mutateAsync({ repo: 'acme/widgets', number: 7 })
    expect(post).toHaveBeenCalledWith('/prs/acme/widgets/7/merge', { method: 'squash' })
    expect(inval).toHaveBeenCalledWith({ queryKey: QK.prDetail('acme/widgets', 7) })
    expect(inval).toHaveBeenCalledWith({ queryKey: QK.sessions })
  })

  it('useSetAutoMerge POSTs {enabled} to /auto-merge', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, error: '' })
    const inval = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useSetAutoMerge(), { wrapper })
    await result.current.mutateAsync({ repo: 'acme/widgets', number: 7, enabled: false })
    expect(post).toHaveBeenCalledWith('/prs/acme/widgets/7/auto-merge', { enabled: false })
    await result.current.mutateAsync({ repo: 'acme/widgets', number: 7, enabled: true })
    expect(post).toHaveBeenLastCalledWith('/prs/acme/widgets/7/auto-merge', { enabled: true })
    expect(inval).toHaveBeenCalledWith({ queryKey: QK.prDetail('acme/widgets', 7) })
    expect(inval).toHaveBeenCalledWith({ queryKey: QK.sessions })
  })
})
