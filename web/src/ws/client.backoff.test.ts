// web/src/ws/client.backoff.test.ts
//
// Reconnect backoff must survive a *flapping* socket. During a deploy restart the
// edge can accept the WebSocket and then immediately drop it (daemon not ready yet).
// If every fleeting `onopen` resets the attempt counter, exponential backoff never
// grows and the client reconnects ~every 500ms — a connect/close storm that flickers
// the "Reconnecting…" banner. The counter must only reset once the connection has
// stayed open past a stability window.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionSocket } from '@/ws/client'

type Internals = {
  ws: { onopen?: () => void; onclose?: (e: Partial<CloseEvent>) => void } | null
  _reconnectAttempt: number
  _reconnectTimer: ReturnType<typeof setTimeout> | null
}

describe('SessionSocket reconnect backoff under a flapping connection', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('keeps backoff growing when the socket opens then closes before the stability window', () => {
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const i = sock as unknown as Internals
    i._reconnectAttempt = 5     // simulate an in-progress reconnect cycle
    i._reconnectTimer = null    // let the next scheduleReconnect run
    i.ws!.onopen?.()            // fleeting open — must NOT reset the counter yet
    vi.advanceTimersByTime(500) // far below the stability window
    i._reconnectTimer = null
    i.ws!.onclose?.({ code: 1006 }) // dropped before stable → backoff must keep growing
    // 5 preserved + 1 from scheduleReconnect. The old behaviour reset to 0 on open,
    // yielding 1 here — which is exactly the storm this guards against.
    expect(i._reconnectAttempt).toBe(6)
    sock.destroy()
  })

  it('resets backoff once the connection stays open past the stability window', () => {
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const i = sock as unknown as Internals
    i._reconnectAttempt = 5
    i.ws!.onopen?.()
    vi.advanceTimersByTime(3500) // past the stability window → reset fires
    expect(i._reconnectAttempt).toBe(0)
    sock.destroy()
  })
})
