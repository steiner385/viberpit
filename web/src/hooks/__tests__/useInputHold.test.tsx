import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ConnectionState } from '@/ws/client'

// A minimal fake of the session socket: just the connection-state emitter the hook
// subscribes to. `null` makes getSessionSocket throw the jsdom SYNTAX_ERR the hook
// degrades on, so the first test keeps covering that path.
type StateListener = (s: ConnectionState) => void
let fakeSocket: {
  connectionState: ConnectionState
  updatePendingState: { active: boolean; count: number }
  listeners: Set<StateListener>
  on: (ev: string, cb: StateListener) => void
  off: (ev: string, cb: StateListener) => void
  emit: (s: ConnectionState) => void
} | null = null

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    getSessionSocket: () => {
      if (!fakeSocket) throw new DOMException('relative ws url', 'SyntaxError')
      return fakeSocket
    },
  }
})

import { useInputHold, RECONNECT_BANNER_GRACE_MS } from '../useInputHold'

function makeSocket(state: ConnectionState): NonNullable<typeof fakeSocket> {
  const listeners = new Set<StateListener>()
  const s: NonNullable<typeof fakeSocket> = {
    connectionState: state,
    updatePendingState: { active: false, count: 0 },
    listeners,
    on: (ev, cb) => { if (ev === 'connectionStateChange') listeners.add(cb) },
    off: (ev, cb) => { if (ev === 'connectionStateChange') listeners.delete(cb) },
    emit: (next) => { s.connectionState = next; for (const cb of listeners) cb(next) },
  }
  return s
}

describe('useInputHold', () => {
  beforeEach(() => { fakeSocket = null })
  afterEach(() => { vi.useRealTimers() })

  it('degrades to not-holding when the socket is unavailable (jsdom)', () => {
    // jsdom rejects the relative '/ws' WebSocket URL, so the hook must degrade safely.
    const { result } = renderHook(() => useInputHold())
    expect(result.current.holding).toBe(false)
    expect(result.current.count).toBe(0)
  })

  // A dropped socket usually comes back on the first backoff attempt, well under a
  // second. Raising the banner for that would push the whole page down and back up
  // again (the banner renders in flow, above the content band), which reads as the
  // transcript flickering under the pointer. Hold the banner back until the outage
  // outlasts a quick reconnect.
  it('does not raise the reconnecting banner for a drop that reconnects within the grace', () => {
    vi.useFakeTimers()
    fakeSocket = makeSocket('connected')
    const { result } = renderHook(() => useInputHold())
    expect(result.current.holding).toBe(false)

    act(() => { fakeSocket!.emit('disconnected'); fakeSocket!.emit('reconnecting') })
    expect(result.current.holding).toBe(false)

    act(() => { vi.advanceTimersByTime(RECONNECT_BANNER_GRACE_MS - 1) })
    expect(result.current.holding).toBe(false)

    act(() => { fakeSocket!.emit('connected') })
    act(() => { vi.advanceTimersByTime(RECONNECT_BANNER_GRACE_MS * 2) })
    expect(result.current.holding).toBe(false)
  })

  it('raises the reconnecting banner once the outage outlasts the grace, and drops it on reconnect', () => {
    vi.useFakeTimers()
    fakeSocket = makeSocket('connected')
    const { result } = renderHook(() => useInputHold())

    act(() => { fakeSocket!.emit('reconnecting') })
    act(() => { vi.advanceTimersByTime(RECONNECT_BANNER_GRACE_MS) })
    expect(result.current.holding).toBe(true)
    expect(result.current.mode).toBe('reconnecting')

    act(() => { fakeSocket!.emit('connected') })
    expect(result.current.holding).toBe(false)
  })

  it('applies the grace to a socket that is already reconnecting at mount', () => {
    vi.useFakeTimers()
    fakeSocket = makeSocket('reconnecting')
    const { result } = renderHook(() => useInputHold())
    expect(result.current.holding).toBe(false)
    act(() => { vi.advanceTimersByTime(RECONNECT_BANNER_GRACE_MS) })
    expect(result.current.holding).toBe(true)
  })
})
