import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useBulkPermanentDeleteSessions, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession, BatchResult } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { post: vi.fn() }, ApiRequestError: class extends Error {} }))

const mk = (id: string): ArchivedSession => ({ id, name: id, account: 'a', cwd: '/x', claudeSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' })

describe('useBulkPermanentDeleteSessions — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes all targeted ids immediately and summarizes partial failure', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b'), mk('c')])
    const result1: BatchResult = { succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] }
    vi.mocked(api.post).mockResolvedValue(result1)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(getToasts()[0]).toMatchObject({ message: 'Deleted 2 · 1 failed', variant: 'error' }))
  })

  it('rolls back all on whole-request failure', async () => {
    const initial = [mk('a'), mk('b')]
    qc.setQueryData(QK.archived, initial)
    vi.mocked(api.post).mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b'])
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual(initial)
      expect(getToasts()[0]).toMatchObject({ variant: 'error' })
    })
  })
})
