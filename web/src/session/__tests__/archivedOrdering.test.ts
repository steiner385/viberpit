import { describe, it, expect } from 'vitest'
import { orderItems } from '../ordering'
import { ARCHIVED_ORDERING } from '../archivedOrdering'
import type { ArchivedSession } from '@/api/types'

const a = (over: Partial<ArchivedSession>): ArchivedSession => ({
  id: 'x', name: 'x', account: 'tony', cwd: '/home/u/GitHub/KinDash',
  claudeSessionId: null, permissionMode: 'default',
  createdAt: '2026-06-01T00:00:00Z', archivedAt: '2026-06-10T00:00:00Z', ...over,
})

describe('ARCHIVED_ORDERING', () => {
  it('sorts by archived date, newest first', () => {
    const rows = [
      a({ id: 'old', archivedAt: '2026-06-01T00:00:00Z' }),
      a({ id: 'new', archivedAt: '2026-06-20T00:00:00Z' }),
    ]
    const g = orderItems(rows, 'none', 'archived', ARCHIVED_ORDERING)
    expect(g[0].sessions.map((s) => s.id)).toEqual(['new', 'old'])
  })

  it('groups by account', () => {
    const rows = [a({ id: '1', account: 'tony' }), a({ id: '2', account: 'work' }), a({ id: '3', account: 'tony' })]
    const g = orderItems(rows, 'account', 'name', ARCHIVED_ORDERING)
    expect(g.map((x) => x.key)).toEqual(['tony', 'work'])
    expect(g[0].sessions.map((s) => s.id)).toEqual(['1', '3'])
  })

  it('groups by repo derived from cwd, scratch last', () => {
    const rows = [
      a({ id: 'k', cwd: '/home/u/GitHub/KinDash' }),
      a({ id: 's', cwd: '' }),
    ]
    const g = orderItems(rows, 'repo', 'name', ARCHIVED_ORDERING)
    expect(g.map((x) => x.label)).toEqual(['KinDash', 'Scratch'])
  })
})
