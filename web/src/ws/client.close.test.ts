// @vitest-environment node
// web/src/ws/client.close.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { clientLogger } from '@/obs/logger'

// The node environment (used here to skip jsdom's ~1.1s-per-file setup cost) has no
// global WebSocket on the Node 20 CI runner, so connect()'s `new WebSocket(url)` throws.
// Stub a minimal constructor; the tests only ever invoke the onopen/onclose/onerror it installs.
class FakeWebSocket {
  static readonly OPEN = 1
  readyState = 0
  onopen: (() => void) | null = null
  onclose: ((e: Partial<CloseEvent>) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: (() => void) | null = null
  url: string
  constructor(url: string) { this.url = url }
  close() {}
  send() {}
}

let realWebSocket: typeof WebSocket | undefined

describe('SessionSocket close logging', () => {
  beforeEach(() => {
    vi.spyOn(clientLogger, 'warn')
    realWebSocket = globalThis.WebSocket
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    vi.restoreAllMocks()
    if (realWebSocket) globalThis.WebSocket = realWebSocket
  })

  it('warns on unexpected close code (1006)', async () => {
    const spy = vi.spyOn(clientLogger, 'warn')
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    // Directly invoke the installed onclose handler with an unexpected code
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    ws.onclose?.({ code: 1006, reason: '' } as CloseEvent)
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining('1006'),
      expect.any(Object),
    )
    sock.destroy()
  })

  it('does NOT warn on normal close code (1000)', async () => {
    const spy = vi.spyOn(clientLogger, 'warn')
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    ws.onclose?.({ code: 1000, reason: '' } as CloseEvent)
    expect(spy).not.toHaveBeenCalledWith(
      expect.stringContaining('code='),
      expect.any(Object),
    )
    sock.destroy()
  })

  it('does NOT warn on navigation close code (1001)', async () => {
    const spy = vi.spyOn(clientLogger, 'warn')
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    ws.onclose?.({ code: 1001, reason: '' } as CloseEvent)
    expect(spy).not.toHaveBeenCalledWith(
      expect.stringContaining('code='),
      expect.any(Object),
    )
    sock.destroy()
  })

  it('warns on ws error', async () => {
    const spy = vi.spyOn(clientLogger, 'warn')
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    ws.onerror?.({} as Event)
    expect(spy).toHaveBeenCalledWith('ws error', expect.any(Object))
    sock.destroy()
  })

  it('emits epochRestart event on close code 1012', async () => {
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    const handler = vi.fn()
    sock.on('epochRestart', handler)
    ws.onclose?.({ code: 1012, reason: 'daemon epoch change' } as CloseEvent)
    expect(handler).toHaveBeenCalledOnce()
    sock.destroy()
  })

  it('does NOT emit epochRestart for other unexpected close codes', async () => {
    const { SessionSocket } = await import('@/ws/client')
    const sock = new SessionSocket('ws://localhost/ws')
    sock.connect()
    const ws = (sock as unknown as { ws: WebSocket | null }).ws as WebSocket
    const handler = vi.fn()
    sock.on('epochRestart', handler)
    ws.onclose?.({ code: 1006, reason: '' } as CloseEvent)
    expect(handler).not.toHaveBeenCalled()
    sock.destroy()
  })
})
