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

  it('serializes interrupt message correctly', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.interrupt('session-1')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'interrupt', sessionId: 'session-1' })
  })

  it('socket.sendInterrupt("s") sends {type:"interrupt",sessionId:"s"}', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.sendInterrupt('s')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'interrupt', sessionId: 's' })
  })

  it('socket.answerQuestion("s","q1",["a"]) sends mode "option" by default', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.answerQuestion('s', 'q1', ['a'])

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'answerQuestion', sessionId: 's', questionId: 'q1', selected: ['a'], mode: 'option' })
  })

  it('socket.answerQuestion returns true when the socket is open', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    expect(sock.answerQuestion('s', 'q1', ['a'])).toBe(true)
    expect(lastSock().sent).toHaveLength(1)
  })

  it('socket.answerQuestion returns false and sends nothing when the socket is not open', () => {
    const sock = new SessionSocket('/ws')
    sock.connect() // CONNECTING — never opened

    expect(sock.answerQuestion('s', 'q1', ['a'])).toBe(false)
    expect(lastSock().sent).toHaveLength(0)
  })

  it('socket.answerQuestion forwards an explicit mode', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.answerQuestion('s', 'q1', ['hello'], 'chat')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'answerQuestion', sessionId: 's', questionId: 'q1', selected: ['hello'], mode: 'chat' })
  })

  it('socket.setPermissionMode("s","plan") sends {type:"setPermissionMode",sessionId:"s",mode:"plan"}', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.setPermissionMode('s', 'plan')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'setPermissionMode', sessionId: 's', mode: 'plan' })
  })

  // ── Inbound messages ─────────────────────────────────────────────────────────

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

  // ── Session events — batch fan-out ────────────────────────────────────────

  it('a batch frame fans out N sessionEvent callbacks in order', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    const received: unknown[] = []
    sock.on('sessionEvent', (e) => received.push(e))

    ws.simulateMessage({
      type: 'batch',
      events: [
        { type: 'turnStart', turnId: 't1' },
        { type: 'textDelta', turnId: 't1', text: 'Hello' },
        { type: 'turnEnd',   turnId: 't1' },
      ],
    })

    expect(received).toHaveLength(3)
    expect((received[0] as { type: string }).type).toBe('turnStart')
    expect((received[1] as { type: string }).type).toBe('textDelta')
    expect((received[2] as { type: string }).type).toBe('turnEnd')
  })

  // ── Outbound: prompt ──────────────────────────────────────────────────────

  it('socket.prompt("s","hi") sends {type:"prompt",sessionId:"s",text:"hi"}', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.prompt('s', 'hi')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'prompt', sessionId: 's', text: 'hi' })
  })

  // ── Outbound: permissionResponse ──────────────────────────────────────────

  it('socket.permissionResponse sends the correct payload', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.permissionResponse('sess-1', 'perm-1', 'allow')

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'permissionResponse', sessionId: 'sess-1', permId: 'perm-1', decision: 'allow' })
  })

  // ── Inbound: individual session event (non-batch) ─────────────────────────

  it('a direct session event frame fires sessionEvent callback', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    const handler = vi.fn()
    sock.on('sessionEvent', handler)

    ws.simulateMessage({ type: 'title', title: 'My Session', source: 'auto' })

    expect(handler).toHaveBeenCalledOnce()
    expect(handler).toHaveBeenCalledWith({ type: 'title', title: 'My Session', source: 'auto' })
  })

  // ── Outbound: cancelQueued ────────────────────────────────────────────────

  it('socket.cancelQueued("s", 2) sends {type:"cancelQueued",sessionId:"s",index:2}', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    sock.cancelQueued('s', 2)

    const sent = parseSent(lastSock(), 0)
    expect(sent).toEqual({ type: 'cancelQueued', sessionId: 's', index: 2 })
  })
})
