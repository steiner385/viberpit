import { describe, it, expect } from 'vitest'
import { revIsNewer, shouldApplyRev, reconcileSessions } from './reconcile'
import type { Session } from '@/api/types'

const s = (id: string, over: Partial<Session> = {}): Session =>
  ({ id, status: 'idle', ...over }) as Session
const T1 = '2021-06-01T12:00:00+00:00'
const T2 = '2021-06-01T12:00:05+00:00'  // 5s later

describe('revIsNewer', () => {
  it('true when candidate strictly newer', () => expect(revIsNewer(T2, T1)).toBe(true))
  it('false when equal', () => expect(revIsNewer(T1, T1)).toBe(false))
  it('false when candidate older', () => expect(revIsNewer(T1, T2)).toBe(false))
  it('false when candidate missing', () => expect(revIsNewer(undefined, T1)).toBe(false))
  it('true when existing missing but candidate present', () => expect(revIsNewer(T1, undefined)).toBe(true))
})

describe('shouldApplyRev', () => {
  it('applies newer', () => expect(shouldApplyRev(T2, T1)).toBe(true))
  it('applies equal', () => expect(shouldApplyRev(T1, T1)).toBe(true))
  it('rejects strictly older', () => expect(shouldApplyRev(T1, T2)).toBe(false))
  it('applies when a token is missing', () => expect(shouldApplyRev(undefined, T1)).toBe(true))
})

describe('reconcileSessions', () => {
  it('returns next as-is when no prev', () => {
    expect(reconcileSessions(undefined, [s('a', { revAt: T1 })])).toEqual([s('a', { revAt: T1 })])
  })
  it('keeps the cached session when its token is newer than the poll', () => {
    const prev = [s('a', { status: 'done', revAt: T2 })]
    const next = [s('a', { status: 'in_progress', revAt: T1 })]  // stale poll
    expect(reconcileSessions(prev, next)[0].status).toBe('done')
  })
  it('takes the poll when it is newer', () => {
    const prev = [s('a', { status: 'in_progress', revAt: T1 })]
    const next = [s('a', { status: 'done', revAt: T2 })]
    expect(reconcileSessions(prev, next)[0].status).toBe('done')
  })
  it('drops a session absent from the poll (poll is authoritative for existence)', () => {
    const prev = [s('a', { revAt: T2 }), s('b', { revAt: T2 })]
    const next = [s('a', { revAt: T1 })]
    const out = reconcileSessions(prev, next)
    expect(out.map((x) => x.id)).toEqual(['a'])
  })
})
