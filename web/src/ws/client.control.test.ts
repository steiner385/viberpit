/**
 * Control-frame rejection tests (#505).
 *
 * When the socket is NOT open, control frames must NOT be silently dropped: each
 * rejecting control method returns `false` AND emits a visible `NOT_CONNECTED`
 * error frame (mirrors the PENDING_BUFFER_FULL emit). When the socket IS open, the
 * frame is written and no error is emitted.
 *
 * attach/detach/prompt are intentionally NOT in this set — they self-heal (re-attach
 * on reopen / prompt queue) and must stay silent (see client.ts and #505 brief).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionSocket, ACTIVITY_THROTTLE_MS } from './client'
import type { WsErrorMsg } from './client'

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = MockWebSocket.CONNECTING
  static instances: MockWebSocket[] = []

  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  sent: string[] = []

  constructor(public url: string) {
    MockWebSocket.instances.push(this)
  }

  send(data: string) { this.sent.push(data) }
  close() { this.readyState = MockWebSocket.CLOSED }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
}

vi.stubGlobal('WebSocket', MockWebSocket)

function lastSock(): MockWebSocket {
  const inst = MockWebSocket.instances
  if (inst.length === 0) throw new Error('No WebSocket instances created')
  return inst[inst.length - 1]
}

// Each entry: the control method invoked, and the frame `type` it should write when open.
const CONTROL_METHODS: { name: string; call: (s: SessionSocket) => boolean; frameType: string }[] = [
  { name: 'interrupt',          call: (s) => s.interrupt('sess'),                           frameType: 'interrupt' },
  { name: 'sendInterrupt',      call: (s) => s.sendInterrupt('sess'),                       frameType: 'interrupt' },
  { name: 'cancelQueued',       call: (s) => s.cancelQueued('sess', 0),                     frameType: 'cancelQueued' },
  { name: 'permissionResponse', call: (s) => s.permissionResponse('sess', 'p1', 'allow'),  frameType: 'permissionResponse' },
  { name: 'setPermissionMode',  call: (s) => s.setPermissionMode('sess', 'plan'),          frameType: 'setPermissionMode' },
  { name: 'answerQuestion',     call: (s) => s.answerQuestion('sess', 'q1', ['a']),         frameType: 'answerQuestion' },
  { name: 'command',            call: (s) => s.command('sess', 'clear', ''),                frameType: 'command' },
]

describe('SessionSocket control-frame rejection (#505)', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
  })

  describe('socket NOT open → reject with visible error, never silently drop', () => {
    for (const m of CONTROL_METHODS) {
      it(`${m.name} returns false and emits NOT_CONNECTED`, () => {
        const sock = new SessionSocket('/ws')
        sock.connect() // CONNECTING — never opened
        const errors: WsErrorMsg[] = []
        sock.on('error', (e) => errors.push(e))

        const result = m.call(sock)

        expect(result).toBe(false)
        expect(lastSock().sent).toHaveLength(0)
        expect(errors).toHaveLength(1)
        expect(errors[0].code).toBe('NOT_CONNECTED')
        expect(errors[0].message).toMatch(/not connected/i)
      })
    }
  })

  describe('socket open → send normally, no error', () => {
    for (const m of CONTROL_METHODS) {
      it(`${m.name} returns true, writes the ${m.frameType} frame, emits no error`, () => {
        const sock = new SessionSocket('/ws')
        sock.connect()
        lastSock().simulateOpen()
        const errors: WsErrorMsg[] = []
        sock.on('error', (e) => errors.push(e))

        const result = m.call(sock)

        expect(result).toBe(true)
        expect(errors).toHaveLength(0)
        expect(lastSock().sent).toHaveLength(1)
        const frame = JSON.parse(lastSock().sent[0]) as { type: string }
        expect(frame.type).toBe(m.frameType)
      })
    }
  })
})

describe('SessionSocket.activity throttling', () => {
  beforeEach(() => { MockWebSocket.instances = [] })

  it('throttles to at most one activity frame per window, silently', () => {
    vi.useFakeTimers()
    try {
      const sock = new SessionSocket('/ws')
      sock.connect()
      lastSock().simulateOpen()
      const errors: WsErrorMsg[] = []
      sock.on('error', (e) => errors.push(e))

      const activityFrames = () =>
        lastSock().sent.filter((s) => (JSON.parse(s) as { type: string }).type === 'activity')

      sock.activity('s1')
      sock.activity('s1')                       // within window → dropped
      expect(activityFrames()).toHaveLength(1)
      expect(errors).toHaveLength(0)            // silent — no NOT_CONNECTED spam

      vi.advanceTimersByTime(ACTIVITY_THROTTLE_MS + 1)
      sock.activity('s1')                       // window elapsed → allowed
      expect(activityFrames()).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops silently when the socket is not open (no error emit)', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()                              // CONNECTING, never opened
    const errors: WsErrorMsg[] = []
    sock.on('error', (e) => errors.push(e))
    sock.activity('s1')
    expect(lastSock().sent).toHaveLength(0)
    expect(errors).toHaveLength(0)
  })
})
