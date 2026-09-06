// web/src/ws/client.liveness.test.ts
// The rev-watermark liveness probe: when the cached session row's revAt (kept fresh
// by the 5s poll / lifecycle patches — channels independent of the transcript
// stream) advances while NO transcript frames are arriving AND the row says the
// session is still running, the transcript stream is demonstrably stale (wedged
// reader, dead forwarding task) and the socket must self-heal via the existing
// detach+attach heal. Total silence produces no seq gaps, so the gap-heal alone can
// never catch this.
//
// The expensive direction is the FALSE positive: a heal is a full replay (attach()
// invalidated the catch-up cursor) plus a warn on the same log line real heals use.
// So most of what follows pins the cases that must NOT heal.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket, LIVENESS_CHECK_MS } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

const T1 = '2026-08-29T12:00:00+00:00'
const T2 = '2026-08-29T12:00:30+00:00'
const T3 = '2026-08-29T12:01:00+00:00'
const T4 = '2026-08-29T12:01:30+00:00'

// ── WebSocket mock (production-wiring tests) ─────────────────────────────────

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

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = MockWebSocket.CLOSED }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
  types(): string[] {
    return this.sent.map((s) => (JSON.parse(s) as { type: string }).type)
  }
}

vi.stubGlobal('WebSocket', MockWebSocket)

type Probe = {
  setQueryClient: (q: QueryClient) => void
  connect: () => void
  destroy: () => void
  attach: (id: string) => void
  _livenessCheck: () => void
  _activeSessionId: string | null
  _connectionState: string
  _syncedRevAt: string | null
  _lastSessionFrameAt: number
  _lastHealAt: number
  _fruitlessProbeHeals: number
  _livenessTimer: ReturnType<typeof setInterval> | null
  _send: (m: object) => boolean
}

function makeSocket(row: Partial<Session>) {
  const qc = new QueryClient()
  const setRow = (patch: Partial<Session>) => {
    qc.setQueryData<Session[]>(QK.sessions, [
      { id: 's1', status: 'in_progress', accountName: 'a', ...row, ...patch } as Session,
    ])
  }
  setRow({})
  const s = new SessionSocket('/ws') as unknown as Probe
  s.setQueryClient(qc)
  s._activeSessionId = 's1'
  s._connectionState = 'connected'
  // Explicit, so the heal cooldown's outcome never depends on the fake-timer epoch
  // (a global vi.setSystemTime(0) would otherwise silently suppress the first heal).
  s._lastHealAt = Date.now() - 60_000
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent, qc, setRow }
}

beforeEach(() => { MockWebSocket.instances = []; vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('SessionSocket rev-watermark liveness probe', () => {
  it('heals when revAt advanced and the stream has been silent past the threshold', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent.map((m) => (m as { type: string }).type)).toEqual(['detach', 'attach'])
  })

  it('does not heal when revAt has not advanced (silent-but-static session)', () => {
    const { s, sent } = makeSocket({ revAt: T1 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  it('does not heal while frames are arriving, even with a newer revAt', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 1_000
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  it('advances the watermark on heal so a static rev cannot re-trigger a loop', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent.length).toBe(2)          // one heal fired
    expect(s._syncedRevAt).toBe(T2)      // watermark advanced
    // Past the heal cooldown, so a second heal is refused by the WATERMARK, not by
    // the rate limiter — without the advance above this call would heal again.
    vi.advanceTimersByTime(3_000)
    s._livenessCheck()                   // same rev again → no second heal
    expect(sent.length).toBe(2)
  })

  it('is inert with no active session or a disconnected socket', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._activeSessionId = null
    s._livenessCheck()
    s._activeSessionId = 's1'
    s._connectionState = 'reconnecting'
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  // ── F1: revAt is a row-write token, not a transcript watermark ──────────────

  it.each(['idle', 'done', 'complete'])(
    'does not heal a resting (%s) session whose rev advanced on a background row write',
    (status) => {
      // updated_at bumps on EVERY persist — auto-name, sub-agent rows, archive/usage
      // bookkeeping — none of which emit a transcript event. On a resting session that
      // is normal, not a stalled stream.
      const { s, sent } = makeSocket({ revAt: T2, status: status as Session['status'] })
      s._syncedRevAt = T1
      s._lastSessionFrameAt = Date.now() - 60_000
      s._livenessCheck()
      expect(sent).toEqual([])
    },
  )

  it('still heals a running session on the same evidence (the status gate is not a mute)', () => {
    const { s, sent } = makeSocket({ revAt: T2, status: 'in_progress' })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent.length).toBe(2)
  })

  it('does not heal when the session row has vanished from the cache', () => {
    const { s, sent, qc } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    qc.setQueryData<Session[]>(QK.sessions, [])
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  // ── F2: a null baseline is the absence of evidence ─────────────────────────

  it('adopts (does not judge) the first cached rev when the baseline is null', () => {
    // Cold deep-link: attach() ran before the sessions query resolved, so the baseline
    // is null — and revIsNewer(anything, null) is true, which used to read as "the rev
    // advanced" the moment the first poll landed.
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = null
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent).toEqual([])
    expect(s._syncedRevAt).toBe(T2)   // baselined instead
  })

  it('heals on the first real advance past an adopted baseline', () => {
    const { s, sent, setRow } = makeSocket({ revAt: T2 })
    s._syncedRevAt = null
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()                // adopts T2
    expect(sent).toEqual([])
    setRow({ revAt: T3 })             // now the rev genuinely advances
    vi.advanceTimersByTime(3_000)
    s._livenessCheck()
    expect(sent.map((m) => (m as { type: string }).type)).toEqual(['detach', 'attach'])
  })

  it('stays quiet while the cache has no rev at all (baseline never established)', () => {
    const { s, sent } = makeSocket({})
    s._syncedRevAt = null
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    s._livenessCheck()
    expect(sent).toEqual([])
    expect(s._syncedRevAt).toBeNull()
  })

  // ── F4: bound the storm ────────────────────────────────────────────────────

  it('gives up after 3 consecutive probe heals that deliver no frames', () => {
    const { s, sent, setRow } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    // The real H4 shape: the poll keeps advancing rev while the stream stays dead.
    for (const rev of [T2, T3, T4]) {
      setRow({ revAt: rev })
      vi.advanceTimersByTime(3_000)
      s._livenessCheck()
    }
    expect(sent.length).toBe(6)        // 3 heals × (detach + attach)
    setRow({ revAt: '2026-08-29T12:02:00+00:00' })
    vi.advanceTimersByTime(3_000)
    s._livenessCheck()
    expect(sent.length).toBe(6)        // budget spent — no 4th heal
  })

  it('restores the probe budget once frames actually arrive', () => {
    const { s, sent, setRow } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    for (const rev of [T2, T3, T4]) {
      setRow({ revAt: rev })
      vi.advanceTimersByTime(3_000)
      s._livenessCheck()
    }
    expect(sent.length).toBe(6)
    s._fruitlessProbeHeals = 0        // what a dispatched batch does
    setRow({ revAt: '2026-08-29T12:02:00+00:00' })
    s._syncedRevAt = T4
    s._lastSessionFrameAt = Date.now() - 60_000
    vi.advanceTimersByTime(3_000)
    s._livenessCheck()
    expect(sent.length).toBe(8)
  })
})

// ── Production wiring: the probe is dead code unless these hold ───────────────

describe('liveness probe wiring', () => {
  function connected(revAt: string) {
    const qc = new QueryClient()
    qc.setQueryData<Session[]>(QK.sessions, [
      { id: 's1', status: 'in_progress', accountName: 'a', revAt } as Session,
    ])
    const sock = new SessionSocket('/ws')
    ;(sock as unknown as Probe).setQueryClient(qc)
    sock.connect()
    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1]
    ws.simulateOpen()
    return { sock, ws, qc, probe: sock as unknown as Probe }
  }

  it('connect() arms the interval and destroy() clears it', () => {
    const { sock, probe } = connected(T1)
    expect(probe._livenessTimer).not.toBeNull()
    let calls = 0
    probe._livenessCheck = () => { calls++ }
    vi.advanceTimersByTime(LIVENESS_CHECK_MS * 3)
    expect(calls).toBe(3)
    sock.destroy()
    expect(probe._livenessTimer).toBeNull()
    vi.advanceTimersByTime(LIVENESS_CHECK_MS * 3)
    expect(calls).toBe(3)
  })

  it('the armed interval heals a silent stream on a running session', () => {
    const { sock, ws, qc, probe } = connected(T1)
    sock.attach('s1')                 // baselines the probe at T1
    expect(ws.types()).toEqual(['attach'])
    qc.setQueryData<Session[]>(QK.sessions, [
      { id: 's1', status: 'in_progress', accountName: 'a', revAt: T2 } as Session,
    ])
    probe._lastHealAt = Date.now() - 60_000
    vi.advanceTimersByTime(20_000)    // past LIVENESS_STALE_MS with no frames
    expect(ws.types()).toEqual(['attach', 'detach', 'attach'])
  })

  it('a dispatched batch re-baselines the watermark and the silence clock', () => {
    const { sock, ws, qc, probe } = connected(T1)
    sock.attach('s1')
    probe._lastSessionFrameAt = Date.now() - 60_000
    qc.setQueryData<Session[]>(QK.sessions, [
      { id: 's1', status: 'in_progress', accountName: 'a', revAt: T2 } as Session,
    ])
    ws.simulateMessage({ type: 'batch', events: [{ type: 'textDelta', turnId: 't1', text: 'hi' }] })
    expect(probe._syncedRevAt).toBe(T2)
    expect(probe._lastSessionFrameAt).toBe(Date.now())
    // …and the probe therefore stays quiet on the very rev it just adopted.
    vi.advanceTimersByTime(20_000)
    expect(ws.types()).toEqual(['attach'])
  })

  it('re-baselines the silence clock on the reconnect re-attach', () => {
    const { sock, ws, probe } = connected(T1)
    sock.attach('s1')
    // Laptop sleep / deploy restart: minutes of stale silence, then the socket returns.
    probe._lastSessionFrameAt = Date.now() - 300_000
    ws.simulateOpen()                 // onopen re-attach path
    expect(probe._lastSessionFrameAt).toBe(Date.now())
  })
})
