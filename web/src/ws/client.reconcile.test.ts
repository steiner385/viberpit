import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import { reconcileSessions } from '@/session/reconcile'
import type { Session } from '@/api/types'

const T1 = '2021-06-01T12:00:00+00:00'
const T2 = '2021-06-01T12:00:05+00:00'
const T3 = '2021-06-01T12:00:10+00:00'
function seed(qc: QueryClient, over: Partial<Session>) {
  qc.setQueryData<Session[]>(QK.sessions, [{ id: 's1', status: 'idle', accountName: 'a', ...over } as Session])
}
function makeSocket() {
  const qc = new QueryClient()
  const s = new SessionSocket('/ws') as unknown as { setQueryClient: (q: QueryClient) => void; _handleInbound: (m: unknown) => void }
  s.setQueryClient(qc)
  return { s, qc }
}
const row = (qc: QueryClient) => (qc.getQueryData<Session[]>(QK.sessions) ?? [])[0]

describe('SessionSocket rev-guarded status patch', () => {
  it('applies a status whose revAt is newer', () => {
    const { s, qc } = makeSocket(); seed(qc, { revAt: T1 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'done', revAt: T2 })
    expect(row(qc).status).toBe('done')
    expect(row(qc).revAt).toBe(T2)
  })
  it('ignores a status whose revAt is strictly older (stale/duplicate frame)', () => {
    const { s, qc } = makeSocket(); seed(qc, { status: 'done', revAt: T2 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress', revAt: T1 })
    expect(row(qc).status).toBe('done')   // unchanged — older frame rejected
    expect(row(qc).revAt).toBe(T2)
  })
})

describe('SessionSocket rev-guarded rotated patch (WS patch surviving stale polls)', () => {
  it('rotated patch applies the new account AND its revAt', () => {
    const { s, qc } = makeSocket(); seed(qc, { accountName: 'old', revAt: T1 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'old', toAccount: 'new', revAt: T2 })
    expect(row(qc).accountName).toBe('new')
    expect(row(qc).revAt).toBe(T2)
  })

  it('ignores a rotated frame whose revAt is strictly older (stale/duplicate frame)', () => {
    const { s, qc } = makeSocket(); seed(qc, { accountName: 'new', revAt: T2 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'old', toAccount: 'new', revAt: T1 })
    expect(row(qc).accountName).toBe('new')
    expect(row(qc).revAt).toBe(T2)
  })

  it('rotated patch survives a poll response snapshotted before the rotation persisted', () => {
    const { s, qc } = makeSocket(); seed(qc, { accountName: 'old', revAt: T1 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'old', toAccount: 'new', revAt: T2 })
    const cached = qc.getQueryData<Session[]>(QK.sessions)!
    // A poll response generated before the rotation committed: still the OLD account,
    // and an older (or equal, pre-rotation) revAt — exactly PR #414's stale-poll shape.
    const stalePoll: Session[] = [{ ...cached[0], accountName: 'old', revAt: T1 }]
    const merged = reconcileSessions(cached, stalePoll)
    expect(merged[0].accountName).toBe('new')
    expect(merged[0].revAt).toBe(T2)
  })

  it('a genuinely newer poll (higher rev, different account) still wins over the WS patch', () => {
    const { s, qc } = makeSocket(); seed(qc, { accountName: 'old', revAt: T1 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'old', toAccount: 'new', revAt: T2 })
    const cached = qc.getQueryData<Session[]>(QK.sessions)!
    // e.g. a second rotation happened and a fresh poll already observed it.
    const newerPoll: Session[] = [{ ...cached[0], accountName: 'newer', revAt: T3 }]
    const merged = reconcileSessions(cached, newerPoll)
    expect(merged[0].accountName).toBe('newer')
    expect(merged[0].revAt).toBe(T3)
  })
})

describe('SessionSocket stall fields', () => {
  it('applies pausedReason/pausedUntil/usageStalled from a status frame', () => {
    const { s, qc } = makeSocket(); seed(qc, { revAt: T1 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'needs_attention', revAt: T2,
      pausedReason: 'all_accounts_busy', pausedUntil: '2026-09-04T16:10:00+00:00', usageStalled: true })
    expect(row(qc).usageStalled).toBe(true)
    expect(row(qc).pausedReason).toBe('all_accounts_busy')
    expect(row(qc).pausedUntil).toBe('2026-09-04T16:10:00+00:00')
  })
  it('a rotated frame clears the stall', () => {
    const { s, qc } = makeSocket()
    seed(qc, { revAt: T1, status: 'needs_attention', usageStalled: true, pausedReason: 'rotation_cap', pausedUntil: T2 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'a', toAccount: 'b', revAt: T3 })
    expect(row(qc).accountName).toBe('b')
    expect(row(qc).usageStalled).toBe(false)
    expect(row(qc).pausedReason).toBeNull()
    expect(row(qc).pausedUntil).toBeNull()
  })
})
