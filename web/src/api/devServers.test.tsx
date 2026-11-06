import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSessionDevServers, DEV_SERVERS_POLL_MS } from './devServers'

interface Reply { status?: number; body: unknown }

function stubFetch(replies: Reply[]) {
  const fn = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () => {
    const r = replies.length > 1 ? replies.shift()! : replies[0]
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const ONE = { host: 'h', servers: [{ port: 5173, address: '127.0.0.1', bind: 'loopback', pid: 1, label: 'vite', open: { kind: 'vnc' } }] }
const NONE = { host: 'h', servers: [] }
const HIDDEN = { requested: false, x11: true }

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useSessionDevServers', () => {
  it('answers the detected servers', async () => {
    const fetchFn = stubFetch([{ body: ONE }])
    const { result } = renderHook(() => useSessionDevServers('S', false, HIDDEN.requested, HIDDEN.x11), { wrapper })
    await waitFor(() => expect(result.current.servers).toHaveLength(1))
    expect(fetchFn).toHaveBeenCalledWith('/api/sessions/S/dev-servers', expect.anything())
  })

  it.each([
    ['detection is switched off', { body: { host: 'h', servers: [], enabled: false } }],
    ['the host cannot say (an old daemon)', { status: 409, body: { detail: { code: 'daemon_outdated' } } }],
    ['the shape is not a list', { body: [] }],
  ])('is null (no section) when %s', async (_why, reply) => {
    const fetchFn = stubFetch([reply])
    const { result } = renderHook(() => useSessionDevServers('S', false, HIDDEN.requested, HIDDEN.x11), { wrapper })
    await waitFor(() => expect(fetchFn).toHaveBeenCalled())
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(result.current.servers).toBeNull()
  })

  it('re-asks when a turn ends', async () => {
    const fetchFn = stubFetch([{ body: NONE }, { body: ONE }])
    const { result, rerender } = renderHook(({ busy }) => useSessionDevServers('S', busy, HIDDEN.requested, HIDDEN.x11), {
      wrapper, initialProps: { busy: true },
    })
    await waitFor(() => expect(result.current.servers).toEqual([]))
    rerender({ busy: false })
    await waitFor(() => expect(result.current.servers).toHaveLength(1))
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('switching sessions fetches the new one once', async () => {
    const fetchFn = stubFetch([{ body: NONE }])
    const { rerender } = renderHook(({ id, busy }) => useSessionDevServers(id, busy, HIDDEN.requested, HIDDEN.x11), {
      wrapper, initialProps: { id: 'A', busy: true },
    })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))
    rerender({ id: 'B', busy: true })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(2))
    expect(fetchFn.mock.calls.map(([u]) => u)).toEqual(['/api/sessions/A/dev-servers', '/api/sessions/B/dev-servers'])
  })

  it.each([
    ['the tab is not shown', { requested: false, x11: true }, NONE, 1],
    ['the tab is asked for but not listed (no X11, nothing detected)', { requested: true, x11: false }, NONE, 1],
    ['the tab is shown on an X11 host', { requested: true, x11: true }, NONE, 3],
    ['the tab is listed for a detected server', { requested: true, x11: false }, ONE, 3],
  ])('polls only while the Browser tab is shown: %s', async (_why, view, body, calls) => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchFn = stubFetch([{ body }])
    // Mid-turn, so the turn-end retries stay out of the count.
    renderHook(() => useSessionDevServers('S', true, view.requested, view.x11), { wrapper })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))
    for (let i = 0; i < 2; i++) await act(async () => { await vi.advanceTimersByTimeAsync(DEV_SERVERS_POLL_MS) })
    expect(fetchFn).toHaveBeenCalledTimes(calls)
  })

  it('after a turn ends, re-asks at once and again at +5 s and +15 s — then stops', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchFn = stubFetch([{ body: NONE }])
    const { rerender } = renderHook(({ busy }) => useSessionDevServers('S', busy, HIDDEN.requested, HIDDEN.x11), {
      wrapper, initialProps: { busy: true },
    })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))
    rerender({ busy: false })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(2))
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(fetchFn).toHaveBeenCalledTimes(3)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(fetchFn).toHaveBeenCalledTimes(4)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(fetchFn).toHaveBeenCalledTimes(4)
  })

  it('a new turn cancels the pending retries', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchFn = stubFetch([{ body: NONE }])
    const { rerender } = renderHook(({ busy }) => useSessionDevServers('S', busy, HIDDEN.requested, HIDDEN.x11), {
      wrapper, initialProps: { busy: false },
    })
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))
    rerender({ busy: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })
})
