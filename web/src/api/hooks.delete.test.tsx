import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { usePermanentDeleteSession, useUnarchiveSession, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))

const arch: ArchivedSession = { id: 'a-1', name: 'Bar', account: 'a', cwd: '/x', claudeSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' }

describe('usePermanentDeleteSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately and toasts on success', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(getToasts()[0]).toMatchObject({ message: 'Deleted Bar' }))
  })

  it('rolls back and error-toasts on failure', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockRejectedValue(new Error('no'))
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([arch])
      expect(getToasts()[0]).toMatchObject({ message: "Couldn't delete Bar", variant: 'error' })
    })
  })
})

describe('useUnarchiveSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately on mutate', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.post).mockResolvedValue(arch)
    const { result } = renderHook(() => useUnarchiveSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
  })
})
