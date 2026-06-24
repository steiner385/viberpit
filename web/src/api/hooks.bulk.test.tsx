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

const mk = (id: string): ArchivedSession => ({ id, name: id, account: 'a', cwd: '/x', claudeSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' })

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

describe('useBulkPermanentDeleteSessions — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes all targeted ids immediately and summarizes partial failure', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b'), mk('c')])
    const result1: BatchResult = { succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] }
    vi.mocked(api.post).mockResolvedValue(result1)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Deleted 2 · 1 failed'))
  })

  it('rolls back all on whole-request failure', async () => {
    const initial = [mk('a'), mk('b')]
    qc.setQueryData(QK.archived, initial)
    vi.mocked(api.post).mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b'])
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual(initial)
      expect(toast.error).toHaveBeenCalledWith("Couldn't delete sessions")
    })
  })
})
