/**
 * Socket channels — the AI drawer runs on its own SessionSocket.
 *
 * `SessionSocket` tracks ONE `_activeSessionId`, so a second consumer sharing the
 * module singleton would steal (and on unmount detach) the route session's
 * attachment. `getSocket(channel)` hands out one instance per channel; every
 * pre-existing call site keeps the 'route' default via `getSessionSocket()`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { getSocket, getSessionSocket, __resetSocketsForTest } from './client'

// getSocket() connects eagerly, and jsdom has no usable WebSocket for a relative
// URL — stub the constructor so these tests exercise the registry, not transport.
class FakeWebSocket {
  static readonly OPEN = 1
  readyState = 0
  url: string
  constructor(url: string) { this.url = url }
  close() {}
  send() {}
  addEventListener() {}
}

let realWebSocket: typeof WebSocket | undefined

beforeEach(() => {
  realWebSocket = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket
  __resetSocketsForTest()
})

afterEach(() => {
  __resetSocketsForTest()
  if (realWebSocket) globalThis.WebSocket = realWebSocket
  vi.restoreAllMocks()
})

describe('socket channels', () => {
  it('route and assistant are distinct singletons; legacy accessor is route', () => {
    const route = getSocket('route')
    const assistant = getSocket('assistant')
    expect(route).not.toBe(assistant)
    expect(getSocket('route')).toBe(route)
    expect(getSocket('assistant')).toBe(assistant)
    expect(getSessionSocket()).toBe(route)
  })

  it('defaults to the route channel', () => {
    expect(getSocket()).toBe(getSocket('route'))
  })

  it('gives each channel its own active session id', () => {
    const route = getSocket('route')
    const assistant = getSocket('assistant')
    route.attach('route-1')
    assistant.attach('assistant-1')
    expect(route.activeSessionId).toBe('route-1')
    expect(assistant.activeSessionId).toBe('assistant-1')
  })

  it('creates the assistant socket lazily — asking for route alone opens one socket', () => {
    const opened: string[] = []
    globalThis.WebSocket = class extends FakeWebSocket {
      constructor(url: string) { super(url); opened.push(url) }
    } as unknown as typeof WebSocket
    getSocket('route')
    getSocket('route')
    expect(opened).toEqual(['/ws'])
    getSocket('assistant')
    expect(opened).toEqual(['/ws', '/ws'])
  })
})
