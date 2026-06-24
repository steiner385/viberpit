import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useCloseSession, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))

const s: Session = { id: 'sess-1', name: 'Foo', accountName: 'a', status: 'idle', createdAt: 'x' }

describe('useCloseSession — optimistic archive', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes the session immediately and shows an Undo toast on success', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    // optimistic removal is synchronous in onMutate
    await waitFor(() => expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([]))
    await waitFor(() => {
      const t = getToasts()
      expect(t).toHaveLength(1)
      expect(t[0]).toMatchObject({ message: 'Archived Foo', variant: 'success' })
      expect(t[0].action?.label).toBe('Undo')
    })
  })

  it('rolls back and shows an error toast on failure', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([s])
      expect(getToasts()[0]).toMatchObject({ message: "Couldn't archive Foo", variant: 'error' })
    })
  })
})
