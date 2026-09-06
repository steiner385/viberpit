// web/src/ws/client.liveness.test.ts
// The rev-watermark liveness probe: when the cached session row's revAt (kept fresh
// by the 5s poll / lifecycle patches — channels independent of the transcript
// stream) advances while NO transcript frames are arriving, the transcript stream
// is demonstrably stale (wedged reader, dead forwarding task) and the socket must
// self-heal via the existing detach+attach heal. Total silence produces no seq
// gaps, so the gap-heal alone can never catch this.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

const T1 = '2026-08-29T12:00:00+00:00'
const T2 = '2026-08-29T12:00:30+00:00'

type Probe = {
  setQueryClient: (q: QueryClient) => void
  _livenessCheck: () => void
  _activeSessionId: string | null
  _connectionState: string
  _syncedRevAt: string | null
  _lastSessionFrameAt: number
  _send: (m: object) => boolean
}

function makeSocket(row: Partial<Session>) {
  const qc = new QueryClient()
  qc.setQueryData<Session[]>(QK.sessions, [
    { id: 's1', status: 'in_progress', accountName: 'a', ...row } as Session,
  ])
  const s = new SessionSocket('/ws') as unknown as Probe
  s.setQueryClient(qc)
  s._activeSessionId = 's1'
  s._connectionState = 'connected'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent }
}

beforeEach(() => { vi.useFakeTimers() })
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
})
