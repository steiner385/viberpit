/**
 * `prompt()` used to return void and silently buffer whenever the socket was closed
 * or a deploy drain was pending. That buffering is correct — a prompt must never be
 * dropped — but it was INVISIBLE: the caller could not tell "on the wire" from "held
 * in this browser until we reconnect", so the transcript showed the same nothing for
 * both. The return value is what lets the pending bubble say which one it is.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionSocket } from './client'
import { getFlight, __resetFlight } from '@/lib/promptFlight'

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static instances: MockWebSocket[] = []

  readyState = MockWebSocket.CONNECTING
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  sent: string[] = []

  url: string

  constructor(url: string) { this.url = url; MockWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = MockWebSocket.CLOSED }

  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
  simulateClose() { this.readyState = MockWebSocket.CLOSED; this.onclose?.(new CloseEvent('close')) }
}

vi.stubGlobal('WebSocket', MockWebSocket)

const lastSock = () => MockWebSocket.instances[MockWebSocket.instances.length - 1]

function openSocket(): SessionSocket {
  const sock = new SessionSocket('/ws')
  sock.connect()
  lastSock().simulateOpen()
  return sock
}

describe('SessionSocket.prompt — send result', () => {
  beforeEach(() => { MockWebSocket.instances = []; vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('returns "sent" when the frame goes out on an open socket', () => {
    const sock = openSocket()
    expect(sock.prompt('s1', 'hello')).toBe('sent')
    expect(JSON.parse(lastSock().sent[0])).toMatchObject({ type: 'prompt', text: 'hello' })
  })

  it('returns "buffered" when the socket is closed', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateClose()
    expect(sock.prompt('s1', 'hello')).toBe('buffered')
  })

  it('returns "buffered" while a deploy drain is pending', () => {
    const sock = openSocket()
    lastSock().simulateMessage({ type: 'updatePending' })
    expect(sock.prompt('s1', 'hello')).toBe('buffered')
    // and nothing went out on the wire
    expect(lastSock().sent.filter((s) => s.includes('"prompt"'))).toHaveLength(0)
  })
})

describe('SessionSocket — promptFlight store', () => {
  beforeEach(() => { MockWebSocket.instances = []; vi.useFakeTimers(); __resetFlight() })
  afterEach(() => { vi.useRealTimers() })

  it('records the send so the session LIST can show it, not just the open transcript', () => {
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    expect(getFlight()['s1']).toBe('sent')
  })

  it('records a held prompt as buffered', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateClose()
    sock.prompt('s1', 'hello')
    expect(getFlight()['s1']).toBe('buffered')
  })

  it('advances the stored stage on an ack', () => {
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    lastSock().simulateMessage({ type: 'promptAck', sessionId: 's1', stage: 'resuming' })
    expect(getFlight()['s1']).toBe('resuming')
  })

  it('returns the stored stage to buffered when the server refuses the prompt', () => {
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    lastSock().simulateMessage({ type: 'promptAck', sessionId: 's1', stage: 'delivered' })
    lastSock().simulateMessage({ type: 'promptRejected', sessionId: 's1', text: 'hello' })
    expect(getFlight()['s1']).toBe('buffered')
  })

  it('clears the session once its status shows the turn started', () => {
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    lastSock().simulateMessage({ type: 'status', sessionId: 's1', status: 'in_progress' })
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('keeps narrating while the session is still at rest', () => {
    // The status broadcasts that arrive DURING the wake-up still say "idle" — clearing
    // on any status at all would blank the pill a moment after it appeared.
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    lastSock().simulateMessage({ type: 'status', sessionId: 's1', status: 'idle' })
    expect(getFlight()['s1']).toBe('sent')
  })

  it('clears a session that has ended rather than stranding the pill', () => {
    const sock = openSocket()
    sock.prompt('s1', 'hello')
    lastSock().simulateMessage({ type: 'status', sessionId: 's1', status: 'ended' })
    expect(getFlight()['s1']).toBeUndefined()
  })
})

describe('SessionSocket — promptAck fan-out', () => {
  beforeEach(() => { MockWebSocket.instances = []; vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('delivers a promptAck frame to promptAck subscribers', () => {
    const sock = openSocket()
    const seen: unknown[] = []
    sock.on('promptAck', (m) => seen.push(m))

    lastSock().simulateMessage({ type: 'promptAck', sessionId: 's1', stage: 'received' })

    expect(seen).toEqual([{ type: 'promptAck', sessionId: 's1', stage: 'received' }])
  })
})
