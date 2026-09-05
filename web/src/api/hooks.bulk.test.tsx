import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useBulkPermanentDeleteSessions, useBulkUnarchiveSessions, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession, BatchResult } from './types'

vi.mock('./client', () => ({ api: { post: vi.fn() }, ApiRequestError: class extends Error {} }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
import { toast } from 'sonner'

const mk = (id: string): ArchivedSession => ({ id, name: id, account: 'a', cwd: '/x', repo: null, engine: 'claude_code', engineSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' })

describe('useBulkUnarchiveSessions — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes all targeted ids from archived immediately on mutate', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b'), mk('c')])
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b', 'c'], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkUnarchiveSessions(), { wrapper })
    result.current.mutate(['a', 'b'])
    await waitFor(() => {
      const remaining = qc.getQueryData<ArchivedSession[]>(QK.archived) ?? []
      expect(remaining.map((s) => s.id)).toEqual(['c'])
    })
  })

  it('toasts Restored <n> on full success', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b')])
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkUnarchiveSessions(), { wrapper })
    result.current.mutate(['a', 'b'])
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Restored 2'))
  })

  it('toasts Restored <n> · <f> failed with variant error on partial failure', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b'), mk('c')])
    const partialResult: BatchResult = { succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] }
    vi.mocked(api.post).mockResolvedValue(partialResult)
    const { result } = renderHook(() => useBulkUnarchiveSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Restored 2 · 1 failed'))
  })
})

describe('useBulkPermanentDeleteSessions — chunked', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('splits ids into chunks of 10', async () => {
    const ids = Array.from({ length: 25 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    vi.mocked(api.post).mockImplementation(async (_url: string, body: unknown) => {
      const chunk = (body as { ids: string[] }).ids
      return { succeeded: chunk, failed: [] } as BatchResult
    })
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(3)
    const sizes = vi.mocked(api.post).mock.calls.map((c) => (c[1] as { ids: string[] }).ids.length)
    expect(sizes).toEqual([10, 10, 5])
  })

  it('removes succeeded ids from the archived cache incrementally (failed ones stay)', async () => {
    qc.setQueryData(QK.archived, ['a', 'b', 'c'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect((qc.getQueryData<ArchivedSession[]>(QK.archived) ?? []).map((s) => s.id)).toEqual(['c'])
  })

  it('toasts a merged partial-failure summary', async () => {
    qc.setQueryData(QK.archived, ['a', 'b', 'c'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Deleted 2 · 1 failed'))
  })

  it('counts a thrown chunk as failed and continues with later chunks', async () => {
    const ids = Array.from({ length: 15 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    vi.mocked(api.post)
      .mockRejectedValueOnce(new Error('500'))
      .mockResolvedValueOnce({ succeeded: ids.slice(10), failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(toast.error).toHaveBeenCalledWith('Deleted 5 · 10 failed')
  })

  it('reports progress and clears it when done', async () => {
    qc.setQueryData(QK.archived, ['a', 'b'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    expect(result.current.progress).toBeNull()
    result.current.mutate(['a', 'b'])
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(result.current.progress).toBeNull()
  })

  it('cancel stops after the current chunk', async () => {
    const ids = Array.from({ length: 15 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    let resolveFirst!: (v: BatchResult) => void
    const first = new Promise<BatchResult>((r) => { resolveFirst = r })
    vi.mocked(api.post).mockReturnValueOnce(first).mockResolvedValue({ succeeded: [], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
    result.current.cancel()
    resolveFirst({ succeeded: ids.slice(0, 10), failed: [] })
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(1)
  })
})
