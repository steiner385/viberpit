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
  noteSent, noteStage, clearFlight, getFlight, subscribeFlight, __resetFlight,
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
