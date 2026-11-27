/**
 * FR-403 trust order needs "is any WS feeding the sessions cache?" inside `api/hooks.ts`,
 * which `ws/client.ts` itself imports (QK) — so the per-channel flags live in
 * `ws/socketState.ts` and the socket registry keeps them current. These tests pin that
 * wiring: a channel's flag is true exactly while its socket is `connected`, dropping the
 * socket (which emits no state change) clears it, and any connected channel counts.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { getSocket, setSessionSocketsAllowed, __resetSocketsForTest } from './client'
import { isAnySocketOpen } from './socketState'

class FakeWebSocket {
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []
  readyState = 0
  url: string
  onopen: (() => void) | null = null
  onclose: ((e: { code: number; reason: string }) => void) | null = null
  onmessage: unknown = null
  onerror: unknown = null
  constructor(url: string) { this.url = url; FakeWebSocket.instances.push(this) }
  close() { this.readyState = 3 }
  send() {}
  addEventListener() {}
  open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.() }
  drop(code = 1006) { this.readyState = 3; this.onclose?.({ code, reason: '' }) }
}

let realWebSocket: typeof WebSocket | undefined

beforeEach(() => {
  FakeWebSocket.instances.length = 0
  realWebSocket = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket
  __resetSocketsForTest()
})

afterEach(() => {
  __resetSocketsForTest()
  if (realWebSocket) globalThis.WebSocket = realWebSocket
})

describe('isAnySocketOpen', () => {
  it('is false before any socket exists', () => {
    expect(isAnySocketOpen()).toBe(false)
  })

  it('is false while connecting and true once the route socket opens', () => {
    getSocket('route')
    expect(isAnySocketOpen()).toBe(false)
    FakeWebSocket.instances[0].open()
    expect(isAnySocketOpen()).toBe(true)
  })

  it('falls back to false when the only socket drops (disconnected → reconnecting)', () => {
    getSocket('route')
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].drop()
    expect(isAnySocketOpen()).toBe(false)
  })

  it('clears when the registry destroys the socket, which emits no state change', () => {
    getSocket('route')
    FakeWebSocket.instances[0].open()
    expect(isAnySocketOpen()).toBe(true)
    setSessionSocketsAllowed(false)
    expect(isAnySocketOpen()).toBe(false)
  })

  it('clears on the test reset too', () => {
    getSocket('route')
    FakeWebSocket.instances[0].open()
    __resetSocketsForTest()
    expect(isAnySocketOpen()).toBe(false)
  })

  it('counts the assistant channel on its own', () => {
    getSocket('assistant')
    FakeWebSocket.instances[0].open()
    expect(isAnySocketOpen()).toBe(true)
  })

  it('stays true while the assistant socket is connected after the route socket dropped', () => {
    getSocket('route')
    getSocket('assistant')
    const [route, assistant] = FakeWebSocket.instances
    route.open()
    assistant.open()
    route.drop()
    expect(isAnySocketOpen()).toBe(true)
  })

  it('is false once both channels are down', () => {
    getSocket('route')
    getSocket('assistant')
    const [route, assistant] = FakeWebSocket.instances
    route.open()
    assistant.open()
    route.drop()
    assistant.drop()
    expect(isAnySocketOpen()).toBe(false)
  })

  it('destroying both channels clears both flags', () => {
    getSocket('route')
    getSocket('assistant')
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[1].open()
    setSessionSocketsAllowed(false)
    expect(isAnySocketOpen()).toBe(false)
  })
})
