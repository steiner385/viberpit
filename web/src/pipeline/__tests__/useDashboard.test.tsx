// Ported from plotroom@5166b52 frontend/src/useDashboard.ts; test suite for the ported hook
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { ApiBaseProvider } from '../ApiBaseContext'
import { useDashboard } from '../useDashboard'

// jsdom has no EventSource — provide a minimal stub that mirrors the real API:
// onopen, onmessage, onerror callbacks + addEventListener for named events.
class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {}

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, fn: (ev: MessageEvent) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }

  close() {}
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <ApiBaseProvider base="/api/pipeline">{children}</ApiBaseProvider>
)

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  // Stub Notification so the hook's notifySupported() returns false (no permission dance)
  vi.stubGlobal('Notification', undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('useDashboard', () => {
  it('connects to <base>/events', () => {
    renderHook(() => useDashboard(), { wrapper })
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toBe('/api/pipeline/events')
  })

  it('applies a full-state frame to state and sets connected', async () => {
    const { result } = renderHook(() => useDashboard(), { wrapper })

    const frame = { generatedAt: '2026-07-12T00:00:00Z', staleSince: null, repos: [] }

    act(() => {
      // Trigger onmessage (the hook wires up onmessage for unnamed SSE frames)
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify(frame) }),
      )
    })

    await waitFor(() => expect(result.current.state).toEqual(frame))
    expect(result.current.connected).toBe(true)
  })

  it('sets connected on onopen', async () => {
    const { result } = renderHook(() => useDashboard(), { wrapper })

    act(() => {
      FakeEventSource.instances[0].onopen?.(new Event('open'))
    })

    await waitFor(() => expect(result.current.connected).toBe(true))
  })

  it('clears connected on onerror', async () => {
    const { result } = renderHook(() => useDashboard(), { wrapper })

    // First connect
    act(() => {
      FakeEventSource.instances[0].onopen?.(new Event('open'))
    })
    await waitFor(() => expect(result.current.connected).toBe(true))

    // Then error
    act(() => {
      FakeEventSource.instances[0].onerror?.(new Event('error'))
    })
    await waitFor(() => expect(result.current.connected).toBe(false))
  })

  it('paints the /snapshot warm frame before any SSE frame arrives', async () => {
    const snap = { generatedAt: 'snap', staleSince: null, repos: [] }
    const fetchImpl = vi.fn(
      async () => ({ ok: true, json: async () => snap }),
    ) as unknown as typeof fetch
    const w = ({ children }: { children: ReactNode }) => (
      <ApiBaseProvider base="/api/pipeline" fetchImpl={fetchImpl}>{children}</ApiBaseProvider>
    )

    const { result } = renderHook(() => useDashboard(), { wrapper: w })

    await waitFor(() => expect(result.current.state).toEqual(snap))
    expect(fetchImpl).toHaveBeenCalledWith('/api/pipeline/snapshot', undefined)
  })

  it('does not let a late /snapshot clobber an SSE frame', async () => {
    let resolveSnap: (v: unknown) => void = () => {}
    const jsonReady = new Promise((res) => { resolveSnap = res })
    const fetchImpl = vi.fn(
      async () => ({ ok: true, json: () => jsonReady }),
    ) as unknown as typeof fetch
    const w = ({ children }: { children: ReactNode }) => (
      <ApiBaseProvider base="/api/pipeline" fetchImpl={fetchImpl}>{children}</ApiBaseProvider>
    )

    const { result } = renderHook(() => useDashboard(), { wrapper: w })

    // Live frame lands first…
    const live = { generatedAt: 'live', staleSince: null, repos: [] }
    act(() => {
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify(live) }),
      )
    })
    await waitFor(() => expect(result.current.state).toEqual(live))

    // …then the slow snapshot resolves — it must be ignored.
    await act(async () => {
      resolveSnap({ generatedAt: 'stale-snap', staleSince: null, repos: [] })
      await Promise.resolve()
    })
    expect(result.current.state).toEqual(live)
  })

  it('staleness: no frame for >90s → stale becomes true', () => {
    vi.useFakeTimers()

    const { result } = renderHook(() => useDashboard(), { wrapper })

    // Deliver a frame to arm the stale timer
    const frame = { generatedAt: '2026-07-12T00:00:00Z', staleSince: null, repos: [] }
    act(() => {
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify(frame) }),
      )
    })

    expect(result.current.stale).toBe(false)

    // Advance past STALE_AFTER_MS (90 000 ms) — triggers the setTimeout callback
    act(() => {
      vi.advanceTimersByTime(90_001)
    })

    expect(result.current.stale).toBe(true)
  })

  it('staleness resets when a new frame arrives', () => {
    vi.useFakeTimers()

    const { result } = renderHook(() => useDashboard(), { wrapper })
    const frame = { generatedAt: '2026-07-12T00:00:00Z', staleSince: null, repos: [] }

    // Arm stale timer then advance 80s (not yet stale)
    act(() => {
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify(frame) }),
      )
    })
    act(() => { vi.advanceTimersByTime(80_000) })

    // New frame resets the timer
    act(() => {
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify({ ...frame, generatedAt: '2026-07-12T00:01:00Z' }) }),
      )
    })

    // Advance another 80s — would have expired under old timer but resets
    act(() => { vi.advanceTimersByTime(80_000) })

    expect(result.current.stale).toBe(false)
  })
})
