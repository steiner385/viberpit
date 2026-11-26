/**
 * The `/ws` gate (#1408 FR-G10.6) — a kiosk or display page never opens a socket.
 *
 * `setSessionSocketsAllowed` is the ONE switch: every hook that reaches for the
 * socket goes through `getSocket`, so these tests count WebSocket constructions
 * rather than trusting any caller to check a flag.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { getSocket, getSessionSocket, setSessionSocketsAllowed, __resetSocketsForTest } from './client'

const opened: string[] = []
const closed: string[] = []

class FakeWebSocket {
  static readonly OPEN = 1
  readyState = 0
  url: string
  constructor(url: string) { this.url = url; opened.push(url) }
  close() { closed.push(this.url) }
  send() {}
  addEventListener() {}
}

let realWebSocket: typeof WebSocket | undefined

beforeEach(() => {
  opened.length = 0
  closed.length = 0
  realWebSocket = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket
  __resetSocketsForTest()
})

afterEach(() => {
  __resetSocketsForTest()
  if (realWebSocket) globalThis.WebSocket = realWebSocket
})

describe('setSessionSocketsAllowed', () => {
  it('connects on first use by default', () => {
    getSessionSocket()
    expect(opened).toEqual(['/ws'])
  })

  it('hands out an unconnected socket while disallowed — no WebSocket is constructed', () => {
    setSessionSocketsAllowed(false)
    const route = getSessionSocket()
    const assistant = getSocket('assistant')
    expect(route).toBe(getSessionSocket())
    expect(route.connectionState).not.toBe('connecting')
    expect(assistant).not.toBe(route)
    expect(opened).toEqual([])
  })

  it('connects the held sockets once allowed', () => {
    setSessionSocketsAllowed(false)
    const held = getSessionSocket()
    setSessionSocketsAllowed(true)
    expect(opened).toEqual(['/ws'])
    expect(getSessionSocket()).toBe(held)
  })

  it('closes an already-open socket when disallowed, and never reopens it', () => {
    const live = getSessionSocket()
    expect(opened).toEqual(['/ws'])
    setSessionSocketsAllowed(false)
    expect(closed).toEqual(['/ws'])
    expect(getSessionSocket()).not.toBe(live)
    expect(opened).toEqual(['/ws'])
  })

  it('is idempotent — repeating the current value constructs nothing', () => {
    setSessionSocketsAllowed(false)
    setSessionSocketsAllowed(false)
    getSessionSocket()
    expect(opened).toEqual([])
  })
})
