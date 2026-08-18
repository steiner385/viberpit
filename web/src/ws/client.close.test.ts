// web/src/ws/client.close.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { clientLogger } from '@/obs/logger'

describe('SessionSocket close logging', () => {
  beforeEach(() => {
    vi.spyOn(clientLogger, 'warn')
  })

  afterEach(() => {
    vi.restoreAllMocks()
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
