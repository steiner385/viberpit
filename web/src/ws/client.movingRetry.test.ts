// @vitest-environment node
// web/src/ws/client.movingRetry.test.ts
//
// D-17: an attach the edge refuses with SESSION_MOVING (the session is between hosts)
// is retried every MOVING_RETRY_MS — re-armed only by another refusal, so an attach
// that succeeds ends the loop — for at most MOVING_RETRY_WINDOW_MS (the move claim's
// lifetime). Once the cached row's `movingTo` clears, the next re-attach is the last.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { QK } from '@/api/hooks'
import { SessionSocket } from './client'

class FakeWS {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = 1
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: unknown) => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  sent: string[] = []
  url: string
  constructor(url: string) { this.url = url }
  send(data: string) { this.sent.push(data) }
  close() {}
}

const realWebSocket = globalThis.WebSocket
const SID = 'moving-1'
const moving = { type: 'error', code: 'SESSION_MOVING', message: 'Moving to rocky…', sessionId: SID }

function setup(movingTo: string | null = 'rocky') {
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWS
  const qc = new QueryClient()
  qc.setQueryData(QK.sessions, [{ id: SID, movingTo }])
  const socket = new SessionSocket('/ws')
  socket.setQueryClient(qc)
  socket.connect()
  const s = socket as unknown as { ws: FakeWS; _handleInbound: (m: unknown) => void }
  socket.attach(SID)
  const attaches = () => s.ws.sent.map((f) => JSON.parse(f) as { type: string }).filter((f) => f.type === 'attach')
  return { socket, qc, s, attaches }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  vi.useRealTimers()
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = realWebSocket
})

describe('SESSION_MOVING attach refusal', () => {
  it('re-attaches after MOVING_RETRY_MS, and again for each further refusal', () => {
    const { socket, s, attaches } = setup()
    expect(attaches()).toHaveLength(1)
    s._handleInbound(moving)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS - 1)
    expect(attaches()).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(attaches()).toHaveLength(2)
    s._handleInbound(moving)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS)
    expect(attaches()).toHaveLength(3)
    socket.destroy()
  })

  it('stops when the attach succeeds (no further refusal re-arms it)', () => {
    const { socket, s, attaches } = setup()
    s._handleInbound(moving)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS)
    expect(attaches()).toHaveLength(2)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS * 10)
    expect(attaches()).toHaveLength(2)
    socket.destroy()
  })

  it('gives up after the claim lifetime', () => {
    const { socket, s, attaches } = setup()
    const rounds = SessionSocket.MOVING_RETRY_WINDOW_MS / SessionSocket.MOVING_RETRY_MS
    // A stuck move: the edge refuses every re-attach the client sends (and only those —
    // a refusal is always the answer to an attach).
    s._handleInbound(moving)
    let seen = attaches().length
    for (let i = 0; i < rounds + 5; i++) {
      vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS)
      if (attaches().length > seen) {
        seen = attaches().length
        s._handleInbound(moving)
      }
    }
    // One public attach plus at most one re-attach per retry interval inside the window.
    expect(attaches().length).toBeLessThanOrEqual(1 + rounds)
    // Given up: nothing re-attaches on its own any more.
    const before = attaches().length
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_WINDOW_MS)
    expect(attaches()).toHaveLength(before)
    socket.destroy()
  })

  it('a refusal after the window gave up opens a fresh window (final review)', () => {
    const { socket, s, attaches } = setup()
    s._handleInbound(moving)
    // The first window's deadline passes (a stuck move, or a long-gone one).
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_WINDOW_MS + SessionSocket.MOVING_RETRY_MS)
    s._handleInbound(moving)                     // past the deadline: gives up, resets
    const i = socket as unknown as { _movingRetryUntil: number }
    expect(i._movingRetryUntil).toBe(0)
    const before = attaches().length
    // Later, the same session refuses again (a fresh attach during a new move).
    s._handleInbound(moving)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS)
    expect(attaches()).toHaveLength(before + 1)
    socket.destroy()
  })

  it('a cleared movingTo makes the next re-attach the last of the window', () => {
    const { socket, s, qc, attaches } = setup()
    s._handleInbound(moving)
    qc.setQueryData(QK.sessions, [{ id: SID, movingTo: null }])
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS)
    expect(attaches()).toHaveLength(2)
    // The window closed: a later refusal opens a fresh one rather than inheriting it.
    const i = socket as unknown as { _movingRetryUntil: number }
    expect(i._movingRetryUntil).toBe(0)
    socket.destroy()
  })

  it('navigating away cancels the pending re-attach', () => {
    const { socket, s, attaches } = setup()
    s._handleInbound(moving)
    socket.detach(SID)
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS * 2)
    expect(attaches()).toHaveLength(1)
    socket.destroy()
  })

  it('ignores a refusal for a session that is not the active one, and other error codes', () => {
    const { socket, s, attaches } = setup()
    s._handleInbound({ ...moving, sessionId: 'other' })
    s._handleInbound({ ...moving, code: 'SESSION_NOT_FOUND' })
    vi.advanceTimersByTime(SessionSocket.MOVING_RETRY_MS * 2)
    expect(attaches()).toHaveLength(1)
    socket.destroy()
  })
})
