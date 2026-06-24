import { describe, it, expect } from 'vitest'
import { orderItems, type OrderingConfig } from '../ordering'

interface Row { id: string; acct: string; t: number }
const CONFIG: OrderingConfig<Row> = {
  idOf: (r) => r.id,
  groupings: {
    acct: { bucketOf: (r) => ({ key: r.acct, label: r.acct }) },
  },
  sorts: {
    time: (a, b) => a.t - b.t,
    id: (a, b) => a.id.localeCompare(b.id),
  },
}

describe('orderItems (generic)', () => {
  const rows: Row[] = [
    { id: 'b', acct: 'y', t: 3 },
    { id: 'a', acct: 'x', t: 1 },
    { id: 'c', acct: 'x', t: 2 },
  ]

  it('groupBy none → single null-label group, sorted', () => {
    const g = orderItems(rows, 'none', 'time', CONFIG)
    expect(g).toHaveLength(1)
    expect(g[0].label).toBeNull()
    expect(g[0].sessions.map((r) => r.id)).toEqual(['a', 'c', 'b'])
  })

  it('groups by config bucket, buckets ordered by label, items keep sort order', () => {
    const g = orderItems(rows, 'acct', 'time', CONFIG)
    expect(g.map((x) => x.key)).toEqual(['x', 'y'])
    expect(g[0].sessions.map((r) => r.id)).toEqual(['a', 'c'])
    expect(g[0].count).toBe(2)
  })

  it('empty input → empty array', () => {
    expect(orderItems([], 'acct', 'time', CONFIG)).toEqual([])
  })
})
