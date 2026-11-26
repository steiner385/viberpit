import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSessions, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { addPendingSession, __resetPending } from '@/lib/pendingSessions'
import { noteSent, getFlight, __resetFlight } from '@/lib/promptFlight'
import { setChannelOpen } from '@/ws/socketState'

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

// FR-403: the poll merges by trust order. A WS-patched cached row (newer revAt) survives
// a stale poll only while the route socket is open; a closed socket cannot refresh that
// row, so the poll outranks it.
describe('useSessions — socket trust order (FR-403)', () => {
  const T1 = '2026-09-08T02:40:00+00:00'
  const T2 = '2026-09-08T02:40:05+00:00'
  const wsPatched: Session = { ...real, status: 'done', revAt: T2 }
  const stalePoll: Session = { ...real, status: 'in_progress', revAt: T1 }
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks(); __resetPending(); __resetFlight()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Seed the cache as a WS patch would have left it, already stale so the mount refetches.
    qc.setQueryData<Session[]>(QK.sessions, [wsPatched], { updatedAt: 0 })
    vi.mocked(api.get).mockResolvedValue([stalePoll])
  })
  afterEach(() => setChannelOpen('route', false))
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('keeps the WS-newer cached row over a stale poll while the socket is open', async () => {
    setChannelOpen('route', true)
    const { result } = renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    await waitFor(() => expect(result.current.isFetching).toBe(false))
    expect(result.current.data?.[0].status).toBe('done')
    expect(result.current.data?.[0].revAt).toBe(T2)
  })

  it('lets the poll overwrite the WS-newer cached row once the socket is closed', async () => {
    setChannelOpen('route', false)
    const { result } = renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(result.current.data?.[0].status).toBe('in_progress'))
    expect(result.current.data?.[0].revAt).toBe(T1)
  })
})
