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

  it('an unranked neighbour gets the moved row’s old rank', () => {
    const rows = [{ slug: 'a', rank: null }, { slug: 'b', rank: 5 }]
    expect(swapRanks(rows, 1, 'up')).toEqual([
      { slug: 'b', priorityRank: 5 },
      { slug: 'a', priorityRank: 5 },
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
