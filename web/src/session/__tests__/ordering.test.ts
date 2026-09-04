import { describe, it, expect } from 'vitest'
import { orderSessions } from '../ordering'
import { sortSessionsByTier } from '../statusTier'
import type { Session, PullRequestInfo } from '@/api/types'

function mk(over: Partial<Session> & { id: string }): Session {
  return {
    name: over.id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
    ...over,
  }
}

function mkWithPr(id: string, status: string, checks: PullRequestInfo['checks']): Session {
  return mk({
    id, status,
    prs: [{ number: 1, url: 'u', state: 'open', checks, title: 't', deployed: false } as PullRequestInfo],
  })
}

const flat = (s: Session[], g: Parameters<typeof orderSessions>[1], so: Parameters<typeof orderSessions>[2]) =>
  orderSessions(s, g, so).flatMap((grp) => grp.sessions).map((x) => x.id)

describe('orderSessions', () => {
  it('parity: none+status equals sortSessionsByTier', () => {
    const s = [
      mk({ id: 'a', status: 'idle' }),
      mk({ id: 'b', status: 'waiting_input' }),
      mk({ id: 'c', status: 'in_progress' }),
      mk({ id: 'd', status: 'needs_attention' }),
    ]
    expect(flat(s, 'none', 'status')).toEqual(sortSessionsByTier(s).map((x) => x.id))
  })

  it('lastActive: newest first, null/invalid sinks to bottom', () => {
    const s = [
      mk({ id: 'old', updatedAt: '2026-06-01T00:00:00Z' }),
      mk({ id: 'new', updatedAt: '2026-06-20T00:00:00Z' }),
      mk({ id: 'none', updatedAt: null }),
    ]
    expect(flat(s, 'none', 'lastActive')).toEqual(['new', 'old', 'none'])
  })

  it('age: oldest created first', () => {
    const s = [
      mk({ id: 'y', createdAt: '2026-06-10T00:00:00Z' }),
      mk({ id: 'x', createdAt: '2026-06-01T00:00:00Z' }),
    ]
    expect(flat(s, 'none', 'age')).toEqual(['x', 'y'])
  })

  it('name: case-insensitive A→Z with id tie-break', () => {
    const s = [mk({ id: '2', name: 'beta' }), mk({ id: '1', name: 'Alpha' })]
    expect(flat(s, 'none', 'name')).toEqual(['1', '2'])
  })

  it('group by status: groups ordered by TIER_RANK with labels', () => {
    const s = [mk({ id: 'busy', status: 'in_progress' }), mk({ id: 'need', status: 'waiting_input' })]
    const g = orderSessions(s, 'status', 'status')
    expect(g.map((x) => x.label)).toEqual(['Needs you', 'Working'])
    expect(g.map((x) => x.count)).toEqual([1, 1])
  })

  it('group by status: parked (waiting_external) forms its own Waiting group, between Done and Working', () => {
    const s = [
      mk({ id: 'busy', status: 'in_progress' }),
      mk({ id: 'wait', status: 'waiting_external' }),
      mk({ id: 'done', status: 'done' }),
    ]
    const g = orderSessions(s, 'status', 'status')
    expect(g.map((x) => x.label)).toEqual(['Done', 'Waiting', 'Working'])
    expect(g.find((x) => x.label === 'Waiting')?.sessions.map((x) => x.id)).toEqual(['wait'])
  })

  it('group by repo: alphabetical, Scratch last', () => {
    const s = [
      mk({ id: 'z', repo: 'zeta/z' }),
      mk({ id: 's', repo: null }),
      mk({ id: 'a', repo: 'alpha/a' }),
    ]
    const g = orderSessions(s, 'repo', 'name')
    expect(g.map((x) => x.label)).toEqual(['alpha/a', 'zeta/z', 'Scratch'])
  })

  it('none → single unlabeled group', () => {
    const g = orderSessions([mk({ id: 'a' })], 'none', 'status')
    expect(g).toHaveLength(1)
    expect(g[0].label).toBeNull()
  })

  it('same-tier: input order preserved (stable sort, no id tie-break)', () => {
    const s = [
      mk({ id: 'z', status: 'idle' }),
      mk({ id: 'a', status: 'idle' }),
    ]
    expect(flat(s, 'none', 'status')).toEqual(['z', 'a'])
  })

  it('done+failing-PR sorts above idle+no-PR (AUT-03)', () => {
    const s = [
      mk({ id: 'idle', status: 'idle' }),
      mkWithPr('broken', 'done', 'failing'),
    ]
    expect(flat(s, 'none', 'status')).toEqual(['broken', 'idle'])
  })

  it('group by status: done+failing-PR lands in error ("Failed") group', () => {
    const s = [
      mk({ id: 'idle', status: 'idle' }),
      mkWithPr('broken', 'done', 'failing'),
    ]
    const g = orderSessions(s, 'status', 'status')
    const errorGroup = g.find((x) => x.label === 'Failed')
    expect(errorGroup?.sessions.map((x) => x.id)).toEqual(['broken'])
    const doneGroup = g.find((x) => x.label === 'Done')
    expect(doneGroup?.sessions.map((x) => x.id)).toEqual(['idle'])
  })
})
