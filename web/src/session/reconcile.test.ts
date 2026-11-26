// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { revIsNewer, shouldApplyRev, reconcileSessions, pollOutranksCache } from './reconcile'
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

// FR-403 trust order: an OPEN socket's WS-patched row outranks the poll when its token is
// newer; once the socket is closed/stale the cache may hold a frame the socket can no
// longer refresh, so the poll outranks it unconditionally.
describe('pollOutranksCache', () => {
  describe('socket open', () => {
    it('cache wins when its token is newer', () => expect(pollOutranksCache(true, T2, T1)).toBe(false))
    it('poll wins when its token is newer', () => expect(pollOutranksCache(true, T1, T2)).toBe(true))
    it('poll wins on an equal token', () => expect(pollOutranksCache(true, T1, T1)).toBe(true))
    it('cache wins when only the cache carries a token', () => expect(pollOutranksCache(true, T1, undefined)).toBe(false))
    it('poll wins when only the poll carries a token', () => expect(pollOutranksCache(true, undefined, T1)).toBe(true))
    it('poll wins when neither carries a token', () => expect(pollOutranksCache(true, undefined, undefined)).toBe(true))
  })
  describe('socket closed', () => {
    it('poll wins even when the cache token is newer', () => expect(pollOutranksCache(false, T2, T1)).toBe(true))
    it('poll wins when it is newer', () => expect(pollOutranksCache(false, T1, T2)).toBe(true))
    it('poll wins when only the cache carries a token', () => expect(pollOutranksCache(false, T1, undefined)).toBe(true))
  })
})

describe('reconcileSessions — socket trust order (FR-403)', () => {
  const wsNewerCache = () => [s('a', { status: 'done', accountName: 'new', revAt: T2 })]
  const stalePoll = () => [s('a', { status: 'in_progress', accountName: 'old', revAt: T1 })]

  it('defaults to an open socket (existing behaviour: WS-newer cache wins)', () => {
    expect(reconcileSessions(wsNewerCache(), stalePoll())[0].status).toBe('done')
  })
  it('socketOpen: true — WS-newer cache wins over the stale poll', () => {
    const out = reconcileSessions(wsNewerCache(), stalePoll(), { socketOpen: true })[0]
    expect(out.status).toBe('done')
    expect(out.accountName).toBe('new')
    expect(out.revAt).toBe(T2)
  })
  it('socketOpen: false — the poll wins even over a WS-newer cached row', () => {
    const out = reconcileSessions(wsNewerCache(), stalePoll(), { socketOpen: false })[0]
    expect(out.status).toBe('in_progress')
    expect(out.accountName).toBe('old')
    expect(out.revAt).toBe(T1)
  })
  it('socketOpen: false — a newer poll still wins (no change from today)', () => {
    const prev = [s('a', { status: 'in_progress', revAt: T1 })]
    const next = [s('a', { status: 'done', revAt: T2 })]
    expect(reconcileSessions(prev, next, { socketOpen: false })[0].status).toBe('done')
  })
  it('socketOpen: false — the poll stays authoritative for existence', () => {
    const prev = [s('a', { revAt: T2 }), s('b', { revAt: T2 })]
    const out = reconcileSessions(prev, [s('a', { revAt: T1 })], { socketOpen: false })
    expect(out.map((x) => x.id)).toEqual(['a'])
  })
  it('socketOpen: false — a poll row without unread still keeps the cached unread (D-10)', () => {
    const prev = [s('a', { status: 'done', revAt: T2, unread: true })]
    const next = [s('a', { status: 'in_progress', revAt: T1 })]
    const out = reconcileSessions(prev, next, { socketOpen: false })[0]
    expect(out.status).toBe('in_progress')
    expect(out.unread).toBe(true)
  })
  it('socketOpen: false — returns the poll row itself (same reference) when nothing needs merging', () => {
    const fresh = s('a', { status: 'in_progress', revAt: T1, unread: false })
    expect(reconcileSessions(wsNewerCache(), [fresh], { socketOpen: false })[0]).toBe(fresh)
  })
})

// D-10: `unread` is per-viewer read state, not part of the session record the rev
// token versions, and only the list payloads carry it. A merge must neither lose
// it (a payload without the field reads as "read") nor let a WS-newer cached row
// hide the poll's fresher value.
describe('reconcileSessions — unread', () => {
  it('keeps the cached unread when the incoming row has none', () => {
    const prev = [s('a', { revAt: T1, unread: true })]
    const next = [s('a', { revAt: T2 })]
    expect(reconcileSessions(prev, next)[0].unread).toBe(true)
  })
  it('takes the poll unread even when the cached row wins on rev token', () => {
    const prev = [s('a', { status: 'done', revAt: T2, unread: false })]
    const next = [s('a', { status: 'in_progress', revAt: T1, unread: true })]
    const out = reconcileSessions(prev, next)[0]
    expect(out.status).toBe('done')
    expect(out.unread).toBe(true)
  })
  it('takes the poll unread when the poll wins', () => {
    const prev = [s('a', { revAt: T1, unread: true })]
    const next = [s('a', { revAt: T2, unread: false })]
    expect(reconcileSessions(prev, next)[0].unread).toBe(false)
  })
  it('returns the cached row unchanged (same reference) when nothing differs', () => {
    const cached = s('a', { revAt: T2, unread: true })
    expect(reconcileSessions([cached], [s('a', { revAt: T1, unread: true })])[0]).toBe(cached)
  })
})
