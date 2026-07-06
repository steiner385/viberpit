// web/src/ws/client.catchup.test.ts
// Wave-3 cursor catch-up (audit W2.3) — the SessionSocket attach-frame behavior on heal and
// on reconnect re-attach: include `sinceSrcSeq` ONLY when the source stream stayed
// contiguous, and dispatch resumeOk so the transcript continues (no replayReset, no dup).
import { describe, it, expect } from 'vitest'
import { SessionSocket } from './client'

function makeSocket() {
  const socket = new SessionSocket('/ws')
  const s = socket as unknown as {
    _activeSessionId: string | null
    _send: (m: object) => boolean
    _handleInbound: (m: unknown) => void
    _healActiveSession: () => void
    _lastHealAt: number
  }
  s._activeSessionId = 's1'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent, socket }
}

const attaches = (sent: object[]) => sent.filter((m) => (m as { type: string }).type === 'attach')

describe('SessionSocket catch-up cursor on heal', () => {
  it('a per-attach seq gap that ALSO broke srcSeq contiguity heals with NO cursor (full replay)', () => {
    const { s, sent } = makeSocket()
    // Establish a clean baseline, then a live event with a srcSeq.
    s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 10 }] })
    // A hole: per-attach seq jumps 0→2 (triggers heal) AND srcSeq jumps 10→12 (breaks contiguity).
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'c', seq: 2, sessionId: 's1', srcSeq: 12 }] })
    const at = attaches(sent)
    expect(at).toHaveLength(1)
    expect(at[0]).not.toHaveProperty('sinceSrcSeq') // contiguity broken → omit cursor → full replay
  })

  it('a seq gap while srcSeq stayed contiguous heals WITH the cursor (catch-up from the ring)', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 10 }] })
    // Force a heal WITHOUT a srcSeq gap: drive a per-attach seq regression via a duplicate
    // manually, then call heal directly (contiguity intact, lastSrcSeq=10).
    ;(s as unknown as { _healActiveSession: () => void })._healActiveSession()
    const at = attaches(sent)
    expect(at).toHaveLength(1)
    expect(at[0]).toMatchObject({ type: 'attach', sessionId: 's1', sinceSrcSeq: 10 })
  })
})

describe('SessionSocket catch-up cursor on reconnect re-attach', () => {
  it('reconnect re-attach carries the cursor when contiguous', () => {
    const { s, socket } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 7 }] })
    // Simulate the reconnect onopen re-attach path (uses the same _attachFrame helper).
    const frame = (socket as unknown as { _attachFrame: (sid: string) => object })._attachFrame('s1')
    expect(frame).toMatchObject({ type: 'attach', sessionId: 's1', sinceSrcSeq: 7 })
  })

  it('omits the cursor entirely for an unseen session (no baseline)', () => {
    const { socket } = makeSocket()
    const frame = (socket as unknown as { _attachFrame: (sid: string) => object })._attachFrame('never-seen')
    expect(frame).toEqual({ type: 'attach', sessionId: 'never-seen' })
  })
})

describe('SessionSocket attach frame carries sinceEpoch alongside the cursor (#497)', () => {
  it('includes sinceEpoch when the cursor was minted under a known epoch', () => {
    const { s, socket } = makeSocket()
    // replayComplete carrying an epoch establishes both the contiguity baseline and the epoch.
    s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: 's1', epoch: 'E7' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 7 }] })
    const frame = (socket as unknown as { _attachFrame: (sid: string) => object })._attachFrame('s1')
    expect(frame).toEqual({ type: 'attach', sessionId: 's1', sinceSrcSeq: 7, sinceEpoch: 'E7' })
  })

  it('omits sinceEpoch (but keeps the cursor) when no epoch was carried', () => {
    const { s, socket } = makeSocket()
    // An old server / local mode: replayComplete without an epoch → contiguity but no epoch.
    s._handleInbound({ type: 'batch', events: [{ type: 'replayComplete', sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1', srcSeq: 3 }] })
    const frame = (socket as unknown as { _attachFrame: (sid: string) => object })._attachFrame('s1')
    expect(frame).toEqual({ type: 'attach', sessionId: 's1', sinceSrcSeq: 3 })
    expect(frame).not.toHaveProperty('sinceEpoch')
  })
})

describe('resumeOk preserves the transcript (no replayReset dispatch)', () => {
  it('does not fan out a replayReset or the resumeOk itself, only the live tail', () => {
    const { s, socket } = makeSocket()
    const seen: string[] = []
    socket.on('sessionEvent', (e) => seen.push((e as { type: string }).type))
    s._handleInbound({ type: 'batch', events: [
      { type: 'resumeOk', sessionId: 's1' },
      { type: 'status', state: 'in_progress', count: 0, sessionId: 's1' },
      { type: 'textDelta', turnId: 't', text: 'tail', seq: 0, sessionId: 's1', srcSeq: 5 },
    ] })
    // No replayReset (transcript continues), resumeOk consumed by transport, tail + status dispatched.
    expect(seen).toEqual(['status', 'textDelta'])
    expect(seen).not.toContain('replayReset')
  })
})
