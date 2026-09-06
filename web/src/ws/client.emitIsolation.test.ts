// @vitest-environment node
// web/src/ws/client.emitIsolation.test.ts
//
// #1310: a throwing listener must not abort the rest of an `_emit` dispatch. The
// loss is otherwise silent AND unhealable: handleLiveBatch has already advanced the
// seq bookkeeping for the whole batch by the time dispatch runs, so the gap-heal —
// which only fires on seq holes — sees a contiguous stream and has nothing to detect.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionSocket } from './client'
import { clientLogger } from '../obs/logger'

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

const delta = (seq: number, text: string) =>
  ({ type: 'textDelta', turnId: 't', text, seq, sessionId: 's1' })

describe('SessionSocket listener isolation', () => {
  // clientLogger is a module singleton — restore between tests so one suite's spy
  // does not inherit the previous test's calls.
  beforeEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })
  afterEach(() => vi.restoreAllMocks())

  it('dispatches the rest of a batch after one sessionEvent listener throws', () => {
    vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    const { s, socket } = makeSocket()
    const seen: string[] = []
    socket.on('sessionEvent', (e) => {
      const ev = e as unknown as { text?: string }
      if (ev.text === 'boom') throw new Error('listener blew up')
      seen.push(ev.text ?? '')
    })
    s._handleInbound({ type: 'batch', events: [delta(0, 'a'), delta(1, 'boom'), delta(2, 'c')] })
    // Event 3 must still arrive: the throw on event 2 is contained to that listener call.
    expect(seen).toEqual(['a', 'c'])
  })

  it('still runs the other listeners for the event the throwing one aborted', () => {
    vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    const { s, socket } = makeSocket()
    const second: string[] = []
    socket.on('sessionEvent', () => { throw new Error('first listener blew up') })
    socket.on('sessionEvent', (e) => second.push((e as unknown as { text: string }).text))
    s._handleInbound({ type: 'batch', events: [delta(0, 'a')] })
    expect(second).toEqual(['a'])
  })

  it('still heals a seq gap in a batch whose dispatch threw', () => {
    // The heal decision is made before dispatch but ACTED ON after it; a propagating
    // listener throw skipped it, so a real hole went unrepaired on top of the drop.
    vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const { s, sent, socket } = makeSocket()
    socket.on('sessionEvent', () => { throw new Error('listener blew up') })
    s._handleInbound({ type: 'batch', events: [delta(0, 'a')] })
    s._handleInbound({ type: 'batch', events: [delta(2, 'c')] }) // hole at seq 1
    expect(sent.filter((m) => (m as { type: string }).type === 'detach')).toHaveLength(1)
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(1)
  })

  it('reports the throw to the client log sink so the drop is not silent', () => {
    const error = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    const { s, socket } = makeSocket()
    socket.on('sessionEvent', () => { throw new Error('listener blew up') })
    s._handleInbound({ type: 'batch', events: [delta(0, 'a')] })
    const calls = error.mock.calls.filter(([m]) => String(m).includes('ws.listener.handler_error'))
    expect(calls).toHaveLength(1)
    expect(String(calls[0][0])).toContain('event=sessionEvent')
    expect(String(calls[0][0])).toContain('listener blew up')
    expect(calls[0][1]).toMatchObject({ sessionId: 's1' })
  })

  it('isolates a throwing listener on a non-sessionEvent channel too', () => {
    const error = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    const { s, socket } = makeSocket()
    const seen: string[] = []
    socket.on('status', () => { throw new Error('status listener blew up') })
    socket.on('status', (m) => seen.push(m.status))
    // A throw here previously escaped _handleInbound before the query-cache patch ran.
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress' })
    expect(seen).toEqual(['in_progress'])
    expect(error.mock.calls.filter(([m]) => String(m).includes('event=status'))).toHaveLength(1)
  })
})

describe('listener error log aggregation', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.useFakeTimers()
    vi.setSystemTime(0)
  })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('logs the first throw immediately, suppresses the rest of the storm, then reports the total', () => {
    // A listener that throws once throws on every event after it. clientLogger's ring
    // holds 200 entries (drop-oldest), so an unthrottled per-event log would evict every
    // other diagnostic in the buffer within a couple of batches.
    const error = vi.spyOn(clientLogger, 'error').mockImplementation(() => {})
    const { s, socket } = makeSocket()
    socket.on('sessionEvent', () => { throw new Error('listener blew up') })
    const handlerErrors = () =>
      error.mock.calls.filter(([m]) => String(m).includes('ws.listener.handler_error'))

    s._handleInbound({ type: 'batch', events: [delta(0, 'a'), delta(1, 'b'), delta(2, 'c')] })
    expect(handlerErrors()).toHaveLength(1)
    expect(String(handlerErrors()[0][0])).toContain('total=1')

    vi.setSystemTime(11_000)
    s._handleInbound({ type: 'batch', events: [delta(3, 'd')] })
    expect(handlerErrors()).toHaveLength(2)
    expect(String(handlerErrors()[1][0])).toContain('total=4')
  })
})
