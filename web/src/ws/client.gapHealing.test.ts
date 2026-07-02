// web/src/ws/client.gapHealing.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionSocket } from './client'

// Build a socket whose _send is spied, with an active session, and drive inbound
// frames through the (test-only) inbound hook.
function makeSocket() {
  const s = new SessionSocket('/ws') as unknown as {
    _activeSessionId: string | null
    _send: (m: object) => boolean
    _handleInbound: (m: unknown) => void
  }
  s._activeSessionId = 's1'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent }
}

describe('SessionSocket gap healing', () => {
  beforeEach(() => vi.useRealTimers())

  it('sends detach then attach (in order) once when a live seq gap is seen', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'c', seq: 2, sessionId: 's1' }] })
    const detaches = sent.filter((m) => (m as { type: string }).type === 'detach')
    const attaches  = sent.filter((m) => (m as { type: string }).type === 'attach')
    // A bare re-attach is a no-op on a live session (server guards with
    // `if session_id in live_tasks: return`).  The detach removes the session
    // from live_tasks first so the subsequent attach re-runs the full replay path.
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
})
