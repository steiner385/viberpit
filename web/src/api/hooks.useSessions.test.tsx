import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSessions } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { addPendingSession, __resetPending } from '@/lib/pendingSessions'

vi.mock('./client', () => ({ api: { get: vi.fn() }, ApiRequestError: class extends Error {} }))

const real: Session = { id: 'sess-1', name: 'Real', accountName: 'a', status: 'idle', createdAt: 'x' }

describe('useSessions — merges pending placeholders', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks(); __resetPending()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('appends pending placeholders to the server list', async () => {
    vi.mocked(api.get).mockResolvedValue([real])
    const { result } = renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(result.current.data).toEqual([real]))

    act(() => { addPendingSession({ name: 'Pending', accountName: '', status: 'in_progress', createdAt: 'y', prs: [] }) })
    await waitFor(() => {
      expect(result.current.data).toHaveLength(2)
      expect(result.current.data?.some((s) => s.pending)).toBe(true)
    })
  })
})
