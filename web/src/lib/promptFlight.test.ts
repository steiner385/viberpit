// @vitest-environment node
/**
 * The session-list half of prompt-receipt feedback.
 *
 * A prompt fired from an open session then navigated away from left the card on the
 * list sitting at a calm "Ready"/"Your turn" for the whole wait. The transcript's
 * pending bubble cannot help there — it is unmounted. This store is the shared truth
 * both surfaces read, written by the socket itself so it survives view changes.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  noteSent, noteStage, clearFlight, reconcileFlight, getFlight, subscribeFlight, __resetFlight,
} from './promptFlight'

describe('promptFlight store', () => {
  beforeEach(() => { __resetFlight() })

  it('records a sent prompt against its session', () => {
    noteSent('s1', 'sent')
    expect(getFlight()['s1']).toBe('sent')
  })

  it('keeps sessions independent', () => {
    noteSent('s1', 'sent')
    noteSent('s2', 'buffered')
    expect(getFlight()).toEqual({ s1: 'sent', s2: 'buffered' })
  })

  it('advances a stage forward', () => {
    noteSent('s1', 'sent')
    noteStage('s1', 'resuming')
    expect(getFlight()['s1']).toBe('resuming')
  })

  it('never walks a stage backwards on a late ack', () => {
    noteSent('s1', 'sent')
    noteStage('s1', 'delivered')
    noteStage('s1', 'received')
    expect(getFlight()['s1']).toBe('delivered')
  })

  it('ignores an ack for a session with nothing in flight', () => {
    noteStage('s1', 'delivered')
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('clears a session once its turn is confirmed', () => {
    noteSent('s1', 'sent')
    clearFlight('s1')
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('returns a stable reference between mutations (useSyncExternalStore contract)', () => {
    noteSent('s1', 'sent')
    expect(getFlight()).toBe(getFlight())
  })

  it('returns a NEW reference after a mutation', () => {
    const before = getFlight()
    noteSent('s1', 'sent')
    expect(getFlight()).not.toBe(before)
  })

  it('does not notify subscribers when nothing actually changed', () => {
    noteSent('s1', 'sent')
    let calls = 0
    subscribeFlight(() => { calls++ })
    noteStage('s1', 'received')
    noteStage('s1', 'received') // same stage again — no-op
    expect(calls).toBe(1)
  })

  it('notifies subscribers on change and stops after unsubscribe', () => {
    let calls = 0
    const unsub = subscribeFlight(() => { calls++ })
    noteSent('s1', 'sent')
    unsub()
    noteSent('s2', 'sent')
    expect(calls).toBe(1)
  })

  it('clearing a session with nothing in flight does not notify', () => {
    let calls = 0
    subscribeFlight(() => { calls++ })
    clearFlight('nobody')
    expect(calls).toBe(0)
  })
})

/**
 * The stuck pill. The only clear signal used to be a live `status` frame with a
 * non-resting status ("the turn started"). That frame is lossy: close the laptop,
 * ride out a deploy restart, or reload the tab while the turn runs and the next
 * thing the browser sees — a `done` status frame, or the 5 s poll — is a RESTING
 * status, which the clear deliberately ignored. Every card the user had prompted
 * before the gap then sat at "Sent — starting…" until the next page reload.
 *
 * `updatedAt` on a session row is the hand-back clock (server: last_turn_at), which
 * advances ONLY when the agent hands output back — never on an incidental row
 * write. So "updatedAt moved past what it was when we sent" is proof the turn we
 * sent has come and gone, and it survives any socket gap because the poll carries it.
 */
describe('promptFlight — reconciling against session rows', () => {
  beforeEach(() => { __resetFlight() })
  const T0 = '2026-09-08T02:40:00+00:00'
  const T1 = '2026-09-08T02:44:00+00:00'

  it('clears a resting session whose hand-back clock advanced past the send baseline', () => {
    noteSent('s1', 'sent', T0)
    noteStage('s1', 'delivered')
    reconcileFlight([{ id: 's1', status: 'done', updatedAt: T1 }])
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('keeps narrating while the resting row still carries the baseline clock', () => {
    // The idle/done broadcasts that arrive DURING a wake-up carry the OLD hand-back
    // time — nothing has come back yet.
    noteSent('s1', 'sent', T0)
    reconcileFlight([{ id: 's1', status: 'done', updatedAt: T0 }])
    expect(getFlight()['s1']).toBe('sent')
  })

  it('treats a first-ever hand-back on a never-run session as an advance', () => {
    noteSent('s1', 'sent', null)
    reconcileFlight([{ id: 's1', status: 'done', updatedAt: T1 }])
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('never judges by time when the baseline was unknown at send', () => {
    // Cold deep-link: the sessions cache had not loaded when the prompt went out, so
    // there is no baseline. Fall back to the status-only rule rather than guess.
    noteSent('s1', 'sent')
    reconcileFlight([{ id: 's1', status: 'done', updatedAt: T1 }])
    expect(getFlight()['s1']).toBe('sent')
  })

  it('still clears on a non-resting status regardless of the clock', () => {
    noteSent('s1', 'sent', T0)
    reconcileFlight([{ id: 's1', status: 'in_progress', updatedAt: T0 }])
    expect(getFlight()['s1']).toBeUndefined()
  })

  it('leaves sessions absent from the rows alone', () => {
    noteSent('s1', 'sent', T0)
    reconcileFlight([{ id: 'other', status: 'done', updatedAt: T1 }])
    expect(getFlight()['s1']).toBe('sent')
  })

  it('does not emit when nothing changed (stable reference for useSyncExternalStore)', () => {
    noteSent('s1', 'sent', T0)
    const before = getFlight()
    reconcileFlight([{ id: 's1', status: 'idle', updatedAt: T0 }])
    expect(getFlight()).toBe(before)
  })
})
