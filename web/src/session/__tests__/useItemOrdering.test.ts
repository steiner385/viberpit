import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useItemOrdering, type OrderingSpec } from '../useSessionOrdering'
import type { OrderingConfig } from '../ordering'

interface Row { id: string; acct: string }
const CONFIG: OrderingConfig<Row> = {
  idOf: (r) => r.id,
  groupings: { acct: { bucketOf: (r) => ({ key: r.acct, label: r.acct }) } },
  sorts: { id: (a, b) => a.id.localeCompare(b.id) },
}
const SPEC: OrderingSpec<'none' | 'acct', 'id', Row> = {
  namespace: 'test', config: CONFIG,
  groupByValues: ['none', 'acct'], sortByValues: ['id'],
  defaultPref: { groupBy: 'none', sortBy: 'id' },
}
const rows: Row[] = [{ id: 'b', acct: 'y' }, { id: 'a', acct: 'x' }]

beforeEach(() => window.localStorage.clear())

describe('useItemOrdering', () => {
  it('orders flat by default and exposes navItems', () => {
    const { result } = renderHook(() => useItemOrdering(rows, SPEC))
    expect(result.current.orderedSessions.map((r) => r.id)).toEqual(['a', 'b'])
  })

  it('regroups and filters by matchedIds', () => {
    const { result } = renderHook(() => useItemOrdering(rows, SPEC, new Set(['a'])))
    act(() => result.current.setGroupBy('acct'))
    expect(result.current.groups.map((g) => g.key)).toEqual(['x'])
  })
})
