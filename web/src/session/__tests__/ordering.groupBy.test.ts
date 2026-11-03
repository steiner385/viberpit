// D-10 group-by: branch, host, date and PR state.
//
// Every date case pins `now` — either passed straight to `orderSessions` or via
// `vi.setSystemTime` for the hook — and builds timestamps with the LOCAL Date
// constructor, so the buckets hold in any timezone and on any calendar day.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { orderSessions, buildNavItems, GROUP_BY_VALUES } from '../ordering'
import { useItemOrdering, LIVE_SPEC, BACKGROUND_SPEC } from '../useSessionOrdering'
import { loadSessionOrder, saveSessionOrder } from '@/lib/sessionOrder'
import type { Session, PullRequestInfo } from '@/api/types'
import { mkSession, pullRequest } from '@/test/factories'

const mk = (id: string, over: Partial<Session> = {}): Session =>
  mkSession(id, {
    accountName: 'a',
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-06-01T00:00:00Z',
    ...over,
  })

const prs = (...states: PullRequestInfo['state'][]): PullRequestInfo[] =>
  states.map((state, i) => pullRequest({ number: i + 1, state }))

/** 2026-06-15 14:30 local time. */
const NOW = new Date(2026, 5, 15, 14, 30, 0)
/** ISO string for a local wall-clock time, offset by `ms`. */
const local = (y: number, mo: number, d: number, h = 0, mi = 0, ms = 0): string =>
  new Date(new Date(y, mo, d, h, mi, 0).getTime() + ms).toISOString()

const summary = (gs: ReturnType<typeof orderSessions>) =>
  gs.map((g) => ({ key: g.key, label: g.label, ids: g.sessions.map((s) => s.id) }))

describe('GroupBy values', () => {
  it('LIVE_SPEC (and Background) offer the four new groupings after the old three', () => {
    expect(GROUP_BY_VALUES).toEqual(['none', 'repo', 'status', 'branch', 'host', 'date', 'pr'])
    expect(LIVE_SPEC.groupByValues).toBe(GROUP_BY_VALUES)
    expect(BACKGROUND_SPEC.groupByValues).toBe(GROUP_BY_VALUES)
  })
})

describe('group by branch', () => {
  it('alphabetical branch groups, then a trailing "No branch" for null/empty', () => {
    const s = [
      mk('z', { branch: 'zz/last-alpha' }),
      mk('n', { branch: null }),
      mk('b', { branch: 'feat/b' }),
      mk('e', { branch: '' }),
      mk('u', {}),
      mk('a', { branch: 'feat/a' }),
      mk('a2', { branch: 'feat/a' }),
    ]
    expect(summary(orderSessions(s, 'branch', 'name'))).toEqual([
      { key: 'feat/a', label: 'feat/a', ids: ['a', 'a2'] },
      { key: 'feat/b', label: 'feat/b', ids: ['b'] },
      { key: 'zz/last-alpha', label: 'zz/last-alpha', ids: ['z'] },
      { key: 'branch:none', label: 'No branch', ids: ['e', 'n', 'u'] },
    ])
  })
})

describe('group by host', () => {
  it('alphabetical host groups, then a trailing "No host"', () => {
    const s = [
      mk('r', { host: 'rocky' }),
      mk('x', { host: null }),
      mk('d', { host: 'dobby' }),
      mk('y', { host: '' }),
    ]
    expect(summary(orderSessions(s, 'host', 'name'))).toEqual([
      { key: 'dobby', label: 'dobby', ids: ['d'] },
      { key: 'rocky', label: 'rocky', ids: ['r'] },
      { key: 'host:none', label: 'No host', ids: ['x', 'y'] },
    ])
  })

  it('"No host" trails even a host that sorts after it alphabetically', () => {
    const s = [mk('x', { host: null }), mk('z', { host: 'zulu' })]
    expect(orderSessions(s, 'host', 'name').map((g) => g.label)).toEqual(['zulu', 'No host'])
  })
})

describe('group by date', () => {
  it('buckets Today / Yesterday / Previous 7 days / Older in that order', () => {
    const s = [
      mk('older', { updatedAt: local(2026, 4, 1) }),
      mk('week', { updatedAt: local(2026, 5, 10, 9) }),
      mk('yest', { updatedAt: local(2026, 5, 14, 23) }),
      mk('today', { updatedAt: local(2026, 5, 15, 8) }),
    ]
    expect(summary(orderSessions(s, 'date', 'name', NOW))).toEqual([
      { key: 'date:today', label: 'Today', ids: ['today'] },
      { key: 'date:yesterday', label: 'Yesterday', ids: ['yest'] },
      { key: 'date:previous-7-days', label: 'Previous 7 days', ids: ['week'] },
      { key: 'date:older', label: 'Older', ids: ['older'] },
    ])
  })

  it('boundaries fall on local midnights', () => {
    const at = (id: string, iso: string) => mk(id, { updatedAt: iso })
    const s = [
      at('today-start', local(2026, 5, 15)), // 00:00 today
      at('yest-end', local(2026, 5, 15, 0, 0, -1)), // 23:59:59.999 yesterday
      at('yest-start', local(2026, 5, 14)),
      at('week-end', local(2026, 5, 14, 0, 0, -1)),
      at('week-start', local(2026, 5, 8)), // 00:00 seven days before today
      at('older-end', local(2026, 5, 8, 0, 0, -1)),
    ]
    const g = orderSessions(s, 'date', 'name', NOW)
    const ids = (label: string) => g.find((x) => x.label === label)?.sessions.map((x) => x.id).sort()
    expect(ids('Today')).toEqual(['today-start'])
    expect(ids('Yesterday')).toEqual(['yest-end', 'yest-start'])
    expect(ids('Previous 7 days')).toEqual(['week-end', 'week-start'])
    expect(ids('Older')).toEqual(['older-end'])
  })

  it('a future timestamp (clock skew) is Today; a missing or invalid one is Older', () => {
    const s = [
      mk('skew', { updatedAt: local(2026, 5, 16, 3) }),
      mk('none', { updatedAt: null }),
      mk('bad', { updatedAt: 'not a date' }),
    ]
    expect(summary(orderSessions(s, 'date', 'name', NOW))).toEqual([
      { key: 'date:today', label: 'Today', ids: ['skew'] },
      { key: 'date:older', label: 'Older', ids: ['bad', 'none'] },
    ])
  })

  it('the hook buckets against the current clock', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      vi.setSystemTime(NOW)
      window.localStorage.setItem('viberpit.sessionOrder', JSON.stringify({ groupBy: 'date', sortBy: 'name' }))
      const { result } = renderHook(() =>
        useItemOrdering([mk('t', { updatedAt: local(2026, 5, 15, 1) }), mk('y', { updatedAt: local(2026, 5, 14, 1) })], LIVE_SPEC),
      )
      expect(result.current.groups.map((g) => g.label)).toEqual(['Today', 'Yesterday'])
    } finally {
      vi.useRealTimers()
      window.localStorage.clear()
    }
  })
})

describe('group by PR state', () => {
  it('orders Open / Draft / Merged / Closed / No PR', () => {
    const s = [
      mk('none', { prs: [] }),
      mk('closed', { prs: prs('closed') }),
      mk('merged', { prs: prs('merged') }),
      mk('draft', { prs: prs('draft') }),
      mk('open', { prs: prs('open') }),
    ]
    expect(summary(orderSessions(s, 'pr', 'name'))).toEqual([
      { key: 'pr:open', label: 'Open', ids: ['open'] },
      { key: 'pr:draft', label: 'Draft', ids: ['draft'] },
      { key: 'pr:merged', label: 'Merged', ids: ['merged'] },
      { key: 'pr:closed', label: 'Closed', ids: ['closed'] },
      { key: 'pr:none', label: 'No PR', ids: ['none'] },
    ])
  })

  it('a session with several PRs files under its most advanced state', () => {
    const s = [
      mk('open-beats-draft', { prs: prs('draft', 'open') }),
      mk('open-beats-merged', { prs: prs('merged', 'open', 'closed') }),
      mk('draft-beats-merged', { prs: prs('merged', 'draft') }),
      mk('merged-beats-closed', { prs: prs('closed', 'merged') }),
    ]
    expect(summary(orderSessions(s, 'pr', 'name'))).toEqual([
      { key: 'pr:open', label: 'Open', ids: ['open-beats-draft', 'open-beats-merged'] },
      { key: 'pr:draft', label: 'Draft', ids: ['draft-beats-merged'] },
      { key: 'pr:merged', label: 'Merged', ids: ['merged-beats-closed'] },
    ])
  })
})

describe('persistence of the new values', () => {
  beforeEach(() => window.localStorage.clear())
  afterEach(() => window.localStorage.clear())

  it.each(['branch', 'host', 'date', 'pr'] as const)('a stored "%s" round-trips', (g) => {
    saveSessionOrder({ groupBy: g, sortBy: 'name' })
    expect(loadSessionOrder()).toEqual({ groupBy: g, sortBy: 'name' })
    const { result } = renderHook(() => useItemOrdering([mk('a')], LIVE_SPEC))
    expect(result.current.groupBy).toBe(g)
  })

  it('a stored unknown value falls back to the default', () => {
    window.localStorage.setItem('viberpit.sessionOrder', JSON.stringify({ groupBy: 'author', sortBy: 'name' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'name' })
    const { result } = renderHook(() => useItemOrdering([mk('a')], LIVE_SPEC))
    expect(result.current.groupBy).toBe('none')
  })
})

describe('buildNavItems follows the new groups', () => {
  beforeEach(() => window.localStorage.clear())

  it('date: one header per bucket in bucket order, collapsed buckets drop their rows', () => {
    const s = [
      mk('o', { updatedAt: local(2026, 0, 1) }),
      mk('t', { updatedAt: local(2026, 5, 15, 9) }),
    ]
    const items = buildNavItems(orderSessions(s, 'date', 'name', NOW), new Set(['date:older']))
    expect(items.map((i) => (i.kind === 'header' ? `h:${i.label}:${i.collapsed}` : i.session.id))).toEqual([
      'h:Today:false', 't', 'h:Older:true',
    ])
  })

  it('pr via the hook: navItems and orderedSessions track the PR groups', () => {
    const { result } = renderHook(() =>
      useItemOrdering([
        mk('n', { prs: [] }),
        mk('m', { prs: prs('merged') }),
        mk('o', { prs: prs('open') }),
      ], LIVE_SPEC),
    )
    act(() => result.current.setGroupBy('pr'))
    expect(result.current.navItems.map((i) => (i.kind === 'header' ? i.label : i.session.id))).toEqual([
      'Open', 'o', 'Merged', 'm', 'No PR', 'n',
    ])
    act(() => result.current.collapseGroup('pr:merged'))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['o', 'n'])
  })

  it('branch via the hook: the No-branch header comes last', () => {
    const { result } = renderHook(() =>
      useItemOrdering([mk('x', { branch: null }), mk('b', { branch: 'main' })], LIVE_SPEC),
    )
    act(() => result.current.setGroupBy('branch'))
    const headers = result.current.navItems.filter((i) => i.kind === 'header')
    expect(headers.map((h) => (h.kind === 'header' ? h.label : ''))).toEqual(['main', 'No branch'])
  })
})
