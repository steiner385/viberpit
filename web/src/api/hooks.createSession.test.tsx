import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useCreateSession, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'

vi.mock('./client', () => ({
  api: { post: vi.fn() },
  ApiRequestError: class extends Error {},
}))

const created: Session = {
  id: 'sess-1', name: 'New Session', accountName: 'a',
  status: 'idle', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
}

describe('useCreateSession — optimistic cache insert', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })

  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }

  it('inserts the created session into the sessions cache on success', async () => {
    qc.setQueryData(QK.sessions, [] as Session[])
    vi.mocked(api.post).mockResolvedValue(created)

    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ initialPrompt: 'go' })

    await waitFor(() => {
      const list = qc.getQueryData<Session[]>(QK.sessions)
      expect(list).toContainEqual(created)
    })
  })

  it('does not duplicate when the session id already exists', async () => {
    qc.setQueryData(QK.sessions, [created] as Session[])
    vi.mocked(api.post).mockResolvedValue(created)

    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ initialPrompt: 'go' })

    await waitFor(() => {
      const list = qc.getQueryData<Session[]>(QK.sessions)
      expect(list?.filter((s) => s.id === 'sess-1')).toHaveLength(1)
    })
  })
})
