// @vitest-environment node
// web/src/ws/client.gapHealing.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionSocket } from './client'
import { clientLogger } from '../obs/logger'

// Build a socket whose _send is spied, with an active session, and drive inbound
// frames through the (test-only) inbound hook.
function makeSocket() {
  const socket = new SessionSocket('/ws')
  const s = socket as unknown as {
    _activeSessionId: string | null
    _send: (m: object) => boolean
    _handleInbound: (m: unknown) => void
  }
  s._activeSessionId = 's1'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent, socket }
}

describe('SessionSocket gap healing', () => {
  beforeEach(() => vi.useRealTimers())

  it('sends detach then attach (in order) once when a live seq gap is seen', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'c', seq: 2, sessionId: 's1' }] })
    const detaches = sent.filter((m) => (m as { type: string }).type === 'detach')
    const attaches  = sent.filter((m) => (m as { type: string }).type === 'attach')
    // The heal sends detach then attach: the detach removes the session from THIS
    // socket's live_tasks and the subsequent attach re-runs the full replay path.
    // (The server also now rebinds a bare re-attach — see _handle_attach — so the
    // attach alone would heal; the explicit detach-first is kept for clear intent.)
    expect(detaches).toHaveLength(1)
    expect(detaches[0]).toMatchObject({ type: 'detach', sessionId: 's1' })
    expect(attaches).toHaveLength(1)
    expect(attaches[0]).toMatchObject({ type: 'attach', sessionId: 's1' })
    // detach must precede attach in the sent sequence
    expect(sent.indexOf(detaches[0])).toBeLessThan(sent.indexOf(attaches[0]))
  })

  it('does not send detach or attach for contiguous events', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'b', seq: 1, sessionId: 's1' }] })
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
    expect(sent.filter((m) => (m as { type: string }).type === 'detach')).toHaveLength(0)
  })

  it('resets the baseline on replayReset so post-reconnect replay does not false-heal', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 9, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'replayReset' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
    expect(sent.filter((m) => (m as { type: string }).type === 'detach')).toHaveLength(0)
  })

  it('a stale old-epoch event BEFORE the replayReset in one batch must not poison the baseline (stale-straddle)', () => {
    // Post-fix server ordering: on detach→attach the batcher flushes stale
    // pre-detach frames FIRST, then the fresh replayReset + replay, often in one
    // batch. The stale seq must be wiped by the in-order reset; the new live
    // epoch then restarts at seq 0 without triggering a heal loop.
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [
      { type: 'textDelta', turnId: 't', text: 'stale', seq: 7, sessionId: 's1' },
      { type: 'replayReset', sessionId: 's1' },
      { type: 'user', turnId: 't', text: 'history', sessionId: 's1' },
      { type: 'replayComplete', sessionId: 's1' },
    ] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'fresh', seq: 0, sessionId: 's1' }] })
    expect(sent.filter((m) => (m as { type: string }).type === 'detach')).toHaveLength(0)
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
  })

  it('records a srcSeq gap via the client logger WITHOUT healing (telemetry only)', () => {
    // srcSeq (the daemon's end-to-end source seq, audit W2.3) is diagnostic: a hole
    // must be logged to the frontend obs sink but must NOT trigger detach/attach.
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 10 }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'b', seq: 1, sessionId: 's1', srcSeq: 12 }] })
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('transcript.srcseq.gap'),
      expect.objectContaining({ sessionId: 's1' }),
    )
    // No heal from the srcSeq gap alone (client seq stayed contiguous 0→1).
    expect(sent.filter((m) => (m as { type: string }).type === 'detach')).toHaveLength(0)
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
    warn.mockRestore()
  })

  it('dispatches a full attach batch (reset, replay, complete, status, permissionRequest) as ordered sessionEvents', () => {
    // Regression pin for the server's batched attach path: every session-scoped
    // frame type it enqueues (including status-with-state and the permission
    // re-emit) must reach the sessionEvent fan-out, in FIFO order, and a batch
    // tagged for another session must be dropped entirely.
    const { s, socket } = makeSocket()
    const seen: string[] = []
    socket.on('sessionEvent', (e) => seen.push((e as { type: string }).type))
    const attachBatch = [
      { type: 'replayReset', sessionId: 's1' },
      { type: 'user', turnId: 't', text: 'hello', sessionId: 's1' },
      { type: 'replayComplete', sessionId: 's1' },
      { type: 'status', state: 'in_progress', count: 0, sessionId: 's1' },
      { type: 'permissionRequest', permId: 'p1', turnId: 't', tool: 'Bash', sessionId: 's1' },
    ]
    s._handleInbound({ type: 'batch', events: attachBatch })
    expect(seen).toEqual(['replayReset', 'user', 'replayComplete', 'status', 'permissionRequest'])

    // Same batch for a session we are not viewing → nothing dispatched.
    seen.length = 0
    s._handleInbound({ type: 'batch', events: attachBatch.map((e) => ({ ...e, sessionId: 's2' })) })
    expect(seen).toEqual([])
  })
})

describe('heal-loop detection', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
  })
  afterEach(() => vi.useRealTimers())

  // Drive one gap-triggering batch (seq jumps by 2 from wherever the baseline is).
  const gapBatch = (seq: number) => ({
    type: 'batch',
    events: [{ type: 'textDelta', turnId: 't', text: 'x', seq, sessionId: 's1' }],
  })

  it('escalates to an error exactly at the 5th heal inside the rolling window', () => {
    const { s } = makeSocket()
    const error = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    // Baseline, then 6 gap-heals spaced past the 2s cooldown (all inside the 60s window).
    s._handleInbound(gapBatch(0))
    for (let i = 1; i <= 6; i++) {
      vi.setSystemTime(i * 2_100)
      s._handleInbound(gapBatch(i * 2)) // seq jumps +2 each time → gap → heal
    }
    const loops = error.mock.calls.filter(([m]) => String(m).includes('transcript.heal.loop'))
    expect(loops).toHaveLength(1) // fired at the threshold crossing only, not per heal
    expect(String(loops[0][0])).toContain('count=5')
    error.mockRestore()
  })

  it('does not escalate for heals spread wider than the window', () => {
    const { s } = makeSocket()
    const error = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    s._handleInbound(gapBatch(0))
    for (let i = 1; i <= 6; i++) {
      vi.setSystemTime(i * 61_000) // each heal falls outside the previous window
      s._handleInbound(gapBatch(i * 2))
    }
    expect(error.mock.calls.filter(([m]) => String(m).includes('transcript.heal.loop')))
      .toHaveLength(0)
    error.mockRestore()
  })
})

describe('srcSeq gap log aggregation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
  })
  afterEach(() => vi.useRealTimers())

  const srcGapBatch = (seq: number, srcSeq: number) => ({
    type: 'batch',
    events: [{ type: 'textDelta', turnId: 't', text: 'x', seq, sessionId: 's1', srcSeq }],
  })

  it('logs the first gap immediately, suppresses further gaps inside the interval, and reports the cumulative total after it', () => {
    const { s } = makeSocket()
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const srcGapWarns = () =>
      warn.mock.calls.filter(([m]) => String(m).includes('transcript.srcseq.gap'))
    s._handleInbound(srcGapBatch(0, 10))
    s._handleInbound(srcGapBatch(1, 12)) // hole 10→12: first gap logs immediately
    expect(srcGapWarns()).toHaveLength(1)
    expect(String(srcGapWarns()[0][0])).toContain('total=1')
    s._handleInbound(srcGapBatch(2, 14)) // hole inside the interval: counted, not logged
    expect(srcGapWarns()).toHaveLength(1)
    vi.setSystemTime(11_000)
    s._handleInbound(srcGapBatch(3, 16)) // past the interval: logs with the running total
    expect(srcGapWarns()).toHaveLength(2)
    expect(String(srcGapWarns()[1][0])).toContain('total=3')
    warn.mockRestore()
  })
})
