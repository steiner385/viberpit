import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useCloseSession, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
import { toast } from 'sonner'

const s: Session = { id: 'sess-1', name: 'Foo', accountName: 'a', status: 'idle', createdAt: 'x' }

describe('useCloseSession — optimistic archive', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes the session immediately and shows an Undo toast on success', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    // optimistic removal is synchronous in onMutate
    await waitFor(() => expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([]))
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith(
        'Archived Foo',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) }),
      )
    })
  })

  it('rolls back and shows an error toast on failure', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([s])
      expect(toast.error).toHaveBeenCalledWith("Couldn't archive Foo")
    })
  })
})
