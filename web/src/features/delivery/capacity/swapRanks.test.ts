import { describe, it, expect } from 'vitest'
import { swapRanks } from './swapRanks'

const products = [
  { slug: 'a', rank: 1 },
  { slug: 'b', rank: 2 },
  { slug: 'c', rank: 3 },
]

describe('swapRanks', () => {
  it('swaps two ranked neighbours moving up', () => {
    expect(swapRanks(products, 1, 'up')).toEqual([
      { slug: 'b', priorityRank: 1 },
      { slug: 'a', priorityRank: 2 },
    ])
  })

  it('swaps two ranked neighbours moving down', () => {
    expect(swapRanks(products, 1, 'down')).toEqual([
      { slug: 'b', priorityRank: 3 },
      { slug: 'c', priorityRank: 2 },
    ])
  })

  it('returns null when the row is already first and moves up', () => {
    expect(swapRanks(products, 0, 'up')).toBeNull()
  })

  it('returns null when the row is already last and moves down', () => {
    expect(swapRanks(products, 2, 'down')).toBeNull()
  })

  it('a ranked row moving past an unranked neighbour falls back to the NEIGHBOUR\'s own position, not the row\'s old rank', () => {
    // Fix round 1 / C1: the middle `?? row.rank` fallback let both sides land
    // on 5 here (a real bug — 'a' and 'b' would tie). Each side gets ONLY the
    // other side's rank, or its own 1-based position — never the other row's
    // stale value.
    const rows = [{ slug: 'a', rank: null }, { slug: 'b', rank: 5 }]
    expect(swapRanks(rows, 1, 'up')).toEqual([
      { slug: 'b', priorityRank: 1 },
      { slug: 'a', priorityRank: 5 },
    ])
  })

  it('promoting an unranked row above a ranked neighbour (coordinator example)', () => {
    // y (index 6, unranked) moves up past x (index 5, rank 5): y takes x's
    // rank (5); x, unranked on the other side of the swap, falls back to y's
    // OLD 1-based position (index 6 + 1 = 7) rather than reusing y's null.
    const rows = Array.from({ length: 7 }, (_, i) => ({ slug: `p${i}`, rank: null as number | null }))
    rows[5] = { slug: 'x', rank: 5 }
    rows[6] = { slug: 'y', rank: null }
    expect(swapRanks(rows, 6, 'up')).toEqual([
      { slug: 'y', priorityRank: 5 },
      { slug: 'x', priorityRank: 7 },
    ])
  })

  it('falls back to 1-based position when both sides are unranked', () => {
    const rows = [
      { slug: 'a', rank: null },
      { slug: 'b', rank: null },
      { slug: 'c', rank: null },
    ]
    // Moving row index 2 ('c') up past index 1 ('b'): 'c' takes 'b's
    // position (2), 'b' takes 'c's old position (3) — a real swap even
    // though neither side ever had a stored rank.
    expect(swapRanks(rows, 2, 'up')).toEqual([
      { slug: 'c', priorityRank: 2 },
      { slug: 'b', priorityRank: 3 },
    ])
  })

  it('out-of-range index yields null', () => {
    expect(swapRanks(products, -1, 'up')).toBeNull()
    expect(swapRanks(products, 3, 'down')).toBeNull()
  })
})
