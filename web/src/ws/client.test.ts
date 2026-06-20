/**
 * Unit tests for SessionSocket (ws/client.ts).
 *
 * WebSocket is mocked globally. Reconnect tests use vitest fake timers.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionSocket } from './client'

// ── WebSocket mock ────────────────────────────────────────────────────────────

class MockWebSocket {
  static CONNECTING = 0
  static OPEN       = 1
  static CLOSING    = 2
  static CLOSED     = 3

  readyState = MockWebSocket.CONNECTING

  // Track all instances created so we can fire events on them
  static instances: MockWebSocket[] = []

  onopen:    ((event: Event) => void) | null    = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror:   ((event: Event) => void) | null    = null
  onclose:   ((event: CloseEvent) => void) | null = null

  sent: string[] = []

  constructor(public url: string) {
    MockWebSocket.instances.push(this)
  }

  send(data: string) { this.sent.push(data) }
  close() { this.readyState = MockWebSocket.CLOSED }

  /** Test helper: simulate a successful open */
  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  /** Test helper: simulate receiving a message */
  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }

  /** Test helper: simulate close */
  simulateClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }
}

vi.stubGlobal('WebSocket', MockWebSocket)

// ── Helpers ───────────────────────────────────────────────────────────────────

function lastSock(): MockWebSocket {
  const inst = MockWebSocket.instances
  if (inst.length === 0) throw new Error('No WebSocket instances created')
  return inst[inst.length - 1]
}

function parseSent(sock: MockWebSocket, index = 0): Record<string, unknown> {
  return JSON.parse(sock.sent[index]) as Record<string, unknown>
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('SessionSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ── Outbound messages ────────────────────────────────────────────────────────

  it('sends attach message when attach() is called', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.attach('session-1')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'attach', sessionId: 'session-1' })
  })

  it('serializes input message correctly', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.input('session-1', 'hello\n')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'input', sessionId: 'session-1', data: 'hello\n' })
  })

  it('serializes resize message correctly', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.resize('session-1', 80, 24)

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'resize', sessionId: 'session-1', cols: 80, rows: 24 })
  })

  it('serializes interrupt message correctly', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.interrupt('session-1')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'interrupt', sessionId: 'session-1' })
  })

  // ── Inbound messages ─────────────────────────────────────────────────────────

  it('invokes output callback with correct data', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    const handler = vi.fn()
    sock.on('output', handler)

    ws.simulateMessage({ type: 'output', sessionId: 'session-1', data: 'hello' })

    expect(handler).toHaveBeenCalledOnce()
    expect(handler).toHaveBeenCalledWith({ type: 'output', sessionId: 'session-1', data: 'hello' })
  })

  it('invokes status callback', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    const handler = vi.fn()
    sock.on('status', handler)

    ws.simulateMessage({ type: 'status', sessionId: 'session-1', status: 'idle' })

    expect(handler).toHaveBeenCalledOnce()
    expect(handler).toHaveBeenCalledWith({ type: 'status', sessionId: 'session-1', status: 'idle' })
  })

  it('invokes rotated callback', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    const handler = vi.fn()
    sock.on('rotated', handler)

    ws.simulateMessage({ type: 'rotated', sessionId: 'session-1', fromAccount: 'acct-a', toAccount: 'acct-b' })

    expect(handler).toHaveBeenCalledOnce()
    expect(handler).toHaveBeenCalledWith({
      type: 'rotated',
      sessionId: 'session-1',
      fromAccount: 'acct-a',
      toAccount: 'acct-b',
    })
  })

  // ── Reconnect behaviour ───────────────────────────────────────────────────────

  it('schedules a reconnect when the socket closes', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()
    const instancesBefore = MockWebSocket.instances.length

    ws.simulateClose()

    // Should not have reconnected immediately
    expect(MockWebSocket.instances.length).toBe(instancesBefore)

    // Advance past the backoff (base 500ms)
    vi.advanceTimersByTime(2000)

    // A new WebSocket should have been created
    expect(MockWebSocket.instances.length).toBeGreaterThan(instancesBefore)
  })

  it('re-sends attach for the active session after reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    // Attach a session so it becomes the "active" one
    sock.attach('session-42')

    // Simulate disconnect
    ws.simulateClose()

    // Advance time to trigger reconnect
    vi.advanceTimersByTime(2000)

    // Simulate the new socket connecting
    const newWs = lastSock()
    newWs.simulateOpen()

    // The re-attach message should have been sent on the new socket
    const reattach = newWs.sent.find((s) => {
      const parsed = JSON.parse(s) as Record<string, unknown>
      return parsed.type === 'attach' && parsed.sessionId === 'session-42'
    })
    expect(reattach).toBeDefined()
  })
})
