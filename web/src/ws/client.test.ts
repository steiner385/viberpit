/**
 * Unit tests for SessionSocket (ws/client.ts).
 *
 * WebSocket is mocked globally. Reconnect tests use vitest fake timers.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'
import { clientLogger } from '@/obs/logger'

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

  url: string

  constructor(url: string) {
    this.url = url
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

  it('patches the cached session record on a status frame (live list update)', () => {
    const qc = new QueryClient()
    qc.setQueryData(QK.sessions, [
      { id: 'session-1', status: 'in_progress' },
      { id: 'session-2', status: 'idle' },
    ])

    const sock = new SessionSocket('/ws')
    sock.setQueryClient(qc)
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    // A broadcast status frame for a session that is NOT the open/attached one must
    // still patch the list cache — this is what keeps the sidebar from going stale.
    ws.simulateMessage({ type: 'status', sessionId: 'session-1', status: 'waiting_input' })

    const sessions = qc.getQueryData(QK.sessions) as Array<{ id: string; status: string }>
    expect(sessions.find((s) => s.id === 'session-1')?.status).toBe('waiting_input')
    expect(sessions.find((s) => s.id === 'session-2')?.status).toBe('idle')
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

describe('SessionSocket update-pending queue', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('buffers prompts while pending and resends them on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'hello', [])
    // Not sent while pending (only the attach frame from open, no prompt frame):
    expect(ws.sent.some((s) => s.includes('"type":"prompt"'))).toBe(false)

    // Daemon restarts: socket drops then reconnects.
    ws.simulateClose()
    sock.connect()
    ws = lastSock()
    ws.simulateOpen()

    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('hello'))).toBe(true)
  })

  it('flushes immediately on updateCancelled (no restart)', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'queued one', [])
    ws.simulateMessage({ type: 'updateCancelled' })

    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('queued one'))).toBe(true)
  })

  it('emits updatePendingChange with active flag and count', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    lastSock().simulateOpen()

    const handler = vi.fn()
    sock.on('updatePendingChange', handler)
    lastSock().simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'a', [])

    expect(handler).toHaveBeenLastCalledWith({ active: true, count: 1 })
  })

  it('emits updatePendingChange {active:false,count:0} after flush on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()
    ws.simulateMessage({ type: 'updatePending' })
    sock.prompt('s1', 'hello', [])
    const handler = vi.fn()
    sock.on('updatePendingChange', handler)
    ws.simulateClose()
    sock.connect()
    ws = lastSock()
    ws.simulateOpen()
    expect(handler).toHaveBeenLastCalledWith({ active: false, count: 0 })
  })

  it('buffers a prompt typed while disconnected (no drain) and flushes it on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()
    sock.attach('s1')

    ws.simulateClose()                       // socket CLOSED, NOT draining
    sock.prompt('s1', 'typed offline', [])   // must buffer, not drop
    expect(sock.updatePendingState.count).toBe(1)

    sock.connect()
    ws = lastSock()
    ws.simulateOpen()
    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('typed offline'))).toBe(true)
  })

  it('drops the oldest held prompt and surfaces an error when the buffer is full', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()
    ws.simulateMessage({ type: 'updatePending' })   // enter holding so prompts buffer

    const errors: { code?: string }[] = []
    sock.on('error', (e) => errors.push(e as { code?: string }))
    for (let i = 0; i < 51; i++) sock.prompt('s1', `m${i}`, [])

    expect(sock.updatePendingState.count).toBe(50)        // capped, not 51
    expect(errors.some((e) => e.code === 'PENDING_BUFFER_FULL')).toBe(true)
  })

  it('re-queues a promptRejected frame and flushes it on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'promptRejected', sessionId: 's1', text: 'refused', attachmentIds: [] })
    expect(sock.updatePendingState).toEqual({ active: true, count: 1 })

    ws.simulateClose()
    sock.connect()
    ws = lastSock()
    ws.simulateOpen()
    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('refused'))).toBe(true)
  })
})

// ── Inbound frame robustness ─────────────────────────────────────────────────
// A frame the socket cannot process must never kill the socket — but it must also
// not vanish. Both failure stages (JSON.parse, and downstream handling) are logged
// under their OWN label, so a report of "the chat got stuck" can be traced to the
// stage that actually threw instead of guessing.

describe('SessionSocket._onRawMessage', () => {
  beforeEach(() => { MockWebSocket.instances = [] })

  type Raw = { _onRawMessage: (d: string) => void }

  it('logs malformed inbound frames instead of dropping them silently', () => {
    const sock = new SessionSocket('/ws') as unknown as Raw
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})

    sock._onRawMessage('{not json')

    // The frame length rides in the message (clientLogger's `extra` is a fixed
    // Partial<ClientLogEntry> envelope — arbitrary keys do not typecheck).
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('ws.message.parse_error'),
      expect.anything(),
    )
    expect(warn.mock.calls[0][0]).toContain('dataLen=9')
    warn.mockRestore()
  })

  it('logs a downstream handler throw under its own label, and keeps the socket alive', () => {
    const sock = new SessionSocket('/ws')
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    // A listener that throws makes the HANDLING stage fail on a perfectly valid
    // frame — labelling that 'parse_error' would send the next debugger hunting a
    // server encoding bug that does not exist.
    sock.on('error', () => { throw new Error('listener boom') })

    expect(() => {
      ;(sock as unknown as Raw)._onRawMessage(
        JSON.stringify({ type: 'error', sessionId: 's1', code: 'X', message: 'y' }),
      )
    }).not.toThrow()

    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('ws.message.handler_error'),
      expect.objectContaining({ sessionId: 's1' }),
    )
    expect(warn).not.toHaveBeenCalledWith(
      expect.stringContaining('parse_error'),
      expect.anything(),
    )
    warn.mockRestore()
  })

  it('never ships the frame body to the log sink when a frame fails to parse', () => {
    const sock = new SessionSocket('/ws') as unknown as Raw
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    // Stand-in for transcript text: a mid-frame corruption (here a trailing comma, the
    // shape an encoding bug produces) makes V8 build its SyntaxError message out of the
    // offending input — it quotes the bytes AROUND the error offset, which in a
    // transcript frame IS transcript text. clientLogger POSTs to /api/client-logs and
    // the server persists it, so nothing derived from the payload may go in.
    const SECRET = 'S3CR3T'
    sock._onRawMessage(`{"type":"batch","events":[{"text":"${SECRET}"},]}`)

    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toContain('ws.message.parse_error')
    // Everything handed to the sink — message AND the extra envelope (`stack` rides
    // through ClientLogger.push untruncated; only `msg` is capped at 4000).
    expect(JSON.stringify(warn.mock.calls)).not.toContain(SECRET)
    // The error KIND survives: the label says which stage threw, dataLen the size.
    expect(warn.mock.calls[0][0]).toContain('kind=SyntaxError')
    warn.mockRestore()
  })
})

// ── Liveness bookkeeping is evidence about the wire, not about the listeners ──

describe('SessionSocket batch liveness bookkeeping', () => {
  const T1 = '2026-08-29T12:00:00+00:00'
  const T2 = '2026-08-29T12:00:30+00:00'

  type Probe = {
    _syncedRevAt: string | null
    _lastSessionFrameAt: number
    _fruitlessProbeHeals: number
  }

  beforeEach(() => { MockWebSocket.instances = []; vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('records the stream as live even when a sessionEvent listener throws mid-batch', () => {
    const qc = new QueryClient()
    const setRow = (revAt: string) => qc.setQueryData<Session[]>(QK.sessions, [
      { id: 's1', status: 'in_progress', accountName: 'a', revAt } as Session,
    ])
    setRow(T1)
    const sock = new SessionSocket('/ws')
    sock.setQueryClient(qc)
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()
    sock.attach('s1')

    const probe = sock as unknown as Probe
    // A stream that has been silent long enough for the probe to be watching, and a rev
    // that has since advanced over the independent poll — the exact evidence pair the
    // liveness probe heals on.
    probe._lastSessionFrameAt = Date.now() - 60_000
    probe._syncedRevAt = T1
    probe._fruitlessProbeHeals = 2
    setRow(T2)

    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let seen = 0
    sock.on('sessionEvent', () => { if (++seen === 2) throw new Error('listener boom') })

    // Frames DID arrive; that a listener blew up on event 2 says nothing about the wire.
    ws.simulateMessage({ type: 'batch', events: [
      { type: 'textDelta', sessionId: 's1', turnId: 't1', text: 'a' },
      { type: 'textDelta', sessionId: 's1', turnId: 't1', text: 'b' },
      { type: 'textDelta', sessionId: 's1', turnId: 't1', text: 'c' },
    ] })

    expect(seen).toBe(2)                                  // the loop did abort…
    expect(probe._lastSessionFrameAt).toBe(Date.now())    // …but the evidence landed
    expect(probe._syncedRevAt).toBe(T2)
    expect(probe._fruitlessProbeHeals).toBe(0)
    // …so no spurious detach+attach heal on the probe's next ticks.
    const before = ws.sent.length
    vi.advanceTimersByTime(20_000)
    expect(ws.sent.slice(before)).toEqual([])
    warn.mockRestore()
  })
})
