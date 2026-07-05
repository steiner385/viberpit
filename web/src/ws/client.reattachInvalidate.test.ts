// web/src/ws/client.reattachInvalidate.test.ts
//
// Incident 2026-07-05, Hole 1 — stale catch-up cursor on reopen into a fresh mount.
// The srcSeq cursor lives in the socket singleton and survives navigation, but a public
// attach(sessionId) represents a FRESH view whose reducer mounts empty. If that attach is
// dropped on a closed socket, the reconnect onopen re-attach must NOT resend the stale
// cursor (which would get resumeOk + tail-only against an empty transcript). A public
// attach therefore invalidates the catch-up cursor for that session first.
//
// These drive the real onopen re-attach path through a controllable fake WebSocket so the
// "dropped on a closed socket, then reopens" window is exercised end-to-end.
import { describe, it, expect, afterEach } from 'vitest'
import { SessionSocket } from './client'

// A fully controllable WebSocket: starts CONNECTING (readyState 0) so _send() drops frames
// (readyState !== OPEN), and exposes hooks to flip to OPEN and fire onopen like a reconnect.
class FakeWS {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = 0
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: unknown) => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  sent: string[] = []
  constructor(public url: string) {}
  send(data: string) { this.sent.push(data) }
  close() {}
}

const realWebSocket = globalThis.WebSocket

afterEach(() => {
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = realWebSocket
})

function connectFake(): { socket: SessionSocket; ws: FakeWS; s: {
  _activeSessionId: string | null
  _handleInbound: (m: unknown) => void
  _gap: { lastSrcSeq: Map<string, number>; srcContiguous: Map<string, boolean> }
} } {
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWS
  const socket = new SessionSocket('/ws')
  socket.connect()
  const s = socket as unknown as {
    ws: FakeWS
    _activeSessionId: string | null
    _handleInbound: (m: unknown) => void
    _gap: { lastSrcSeq: Map<string, number>; srcContiguous: Map<string, boolean> }
  }
  return { socket, ws: s.ws, s }
}

/** Fire the reconnect onopen and return the attach frames the socket sent during it. */
function reopenAndCollectAttaches(ws: FakeWS): Array<Record<string, unknown>> {
  ws.sent.length = 0
  ws.readyState = FakeWS.OPEN
  ws.onopen?.({})
  return ws.sent.map((d) => JSON.parse(d)).filter((m) => m.type === 'attach')
}

function establishCursor(s: { _handleInbound: (m: unknown) => void }, sid: string, srcSeq: number) {
  s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: sid }] })
  s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: sid, srcSeq }] })
}

describe('Hole 1 — public attach invalidates the stale catch-up cursor', () => {
  it('reopen re-attach after a swallowed fresh attach carries NO sinceSrcSeq (full replay)', () => {
    const { socket, ws, s } = connectFake()
    // A cursor is live for s1 from a prior contiguous stream.
    s._activeSessionId = 's1'
    establishCursor(s, 's1', 5)

    // Navigate away then back: detach (dropped — socket CONNECTING) then a fresh public
    // attach (also dropped). The fresh attach must invalidate the cursor.
    socket.detach('s1')
    socket.attach('s1')

    // Socket reopens → onopen re-attaches the active session. It must NOT resend the cursor.
    const attaches = reopenAndCollectAttaches(ws)
    expect(attaches).toHaveLength(1)
    expect(attaches[0]).not.toHaveProperty('sinceSrcSeq')
    expect(attaches[0]).toEqual({ type: 'attach', sessionId: 's1' })
    socket.destroy()
  })
})

describe('mid-view reconnect regression — cursor preserved without a public attach', () => {
  it('reopen re-attach carries sinceSrcSeq when there was NO intervening public attach()', () => {
    const { socket, ws, s } = connectFake()
    // The view is already attached and streaming (cursor contiguous); the socket then drops
    // and reconnects with NO public attach() in between (the legitimate resumeOk path).
    s._activeSessionId = 's1'
    establishCursor(s, 's1', 7)

    const attaches = reopenAndCollectAttaches(ws)
    expect(attaches).toHaveLength(1)
    expect(attaches[0]).toMatchObject({ type: 'attach', sessionId: 's1', sinceSrcSeq: 7 })
    socket.destroy()
  })
})

describe('attach() clears the cursor for that session only', () => {
  it('leaves other sessions’ cursors intact', () => {
    const { socket, s } = connectFake()
    establishCursor(s, 's1', 11)
    establishCursor(s, 's2', 22)

    socket.attach('s1')

    expect(s._gap.lastSrcSeq.has('s1')).toBe(false)
    expect(s._gap.srcContiguous.has('s1')).toBe(false)
    expect(s._gap.lastSrcSeq.get('s2')).toBe(22)
    expect(s._gap.srcContiguous.get('s2')).toBe(true)
    socket.destroy()
  })
})
