import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSessions } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { addPendingSession, __resetPending } from '@/lib/pendingSessions'
import { noteSent, getFlight, __resetFlight } from '@/lib/promptFlight'

vi.mock('./client', () => ({ api: { get: vi.fn() }, ApiRequestError: class extends Error {} }))

const real: Session = { id: 'sess-1', name: 'Real', accountName: 'a', engine: 'claude_code', engineSessionId: null, status: 'idle', createdAt: 'x', prs: [] }

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

    act(() => { addPendingSession({ name: 'Pending', accountName: '', engine: 'claude_code', engineSessionId: null, status: 'in_progress', createdAt: 'y', prs: [] }) })
    await waitFor(() => {
      expect(result.current.data).toHaveLength(2)
      expect(result.current.data?.some((s) => s.pending)).toBe(true)
    })
  })
})

describe('useSessions — reconciles the prompt-flight store against the poll', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks(); __resetPending(); __resetFlight()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('clears a "Sent — starting…" entry once the poll shows the turn has handed back', async () => {
    // The socket was down while the turn ran, so no `in_progress` frame ever arrived.
    // The 5 s poll is the durable signal: the row's hand-back clock moved past the
    // value captured at send time.
    noteSent('sess-1', 'sent', '2026-09-08T02:40:00+00:00')
    vi.mocked(api.get).mockResolvedValue([{ ...real, status: 'done', updatedAt: '2026-09-08T02:44:00+00:00' }])
    renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(getFlight()['sess-1']).toBeUndefined())
  })

  it('leaves an entry alone while the poll still carries the pre-send clock', async () => {
    noteSent('sess-1', 'sent', '2026-09-08T02:40:00+00:00')
    vi.mocked(api.get).mockResolvedValue([{ ...real, status: 'done', updatedAt: '2026-09-08T02:40:00+00:00' }])
    const { result } = renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(result.current.data).toHaveLength(1))
    expect(getFlight()['sess-1']).toBe('sent')
  })
})
