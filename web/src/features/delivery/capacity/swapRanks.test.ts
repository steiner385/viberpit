import { describe, it, expect } from 'vitest'
import { swapRanks, type RankInput } from './swapRanks'

const products = [
  { slug: 'a', rank: 1 },
  { slug: 'b', rank: 2 },
  { slug: 'c', rank: 3 },
]

/** Apply patches (in order — later entries for the same slug win) to `rows`
 *  and return the slugs sorted by their FINAL rank, so a test can assert the
 *  resulting order rather than just the two literal patch values. */
function applyAndOrder(rows: readonly RankInput[], patches: readonly { slug: string; priorityRank: number }[]) {
  const byRank = new Map(rows.map((r) => [r.slug, r.rank] as const))
  for (const p of patches) byRank.set(p.slug, p.priorityRank)
  return [...byRank.entries()].sort((x, y) => (x[1] ?? Infinity) - (y[1] ?? Infinity)).map(([slug]) => slug)
}

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
    // stale value. Only two rows exist, so there is nothing else to normalise.
    const rows = [{ slug: 'a', rank: null }, { slug: 'b', rank: 5 }]
    expect(swapRanks(rows, 1, 'up')).toEqual([
      { slug: 'b', priorityRank: 1 },
      { slug: 'a', priorityRank: 5 },
    ])
  })

  it('a fully ranked list still yields exactly two patches — no normalisation noise', () => {
    // Fix round 3: normalisation only fires when EITHER side of the swap is
    // itself unranked. A clean, sequential list (every deployment ends up
    // here once ranking starts) must not grow extra patches for rows that
    // never moved.
    expect(swapRanks(products, 1, 'up')).toHaveLength(2)
  })

  it('promoting an unranked row above a ranked neighbour in an otherwise fully ranked list yields the pair plus normalisation for any other unranked row', () => {
    // Coordinator example, fix round 3: y (unranked) moves up past x (rank 5).
    // p0/p1/p3/p4 are already ranked AND already match their displayed
    // position, so normalising them is a no-op — only p2, the one other
    // unranked row, needs its own patch. x is part of the swap pair, so it is
    // excluded from the normalisation pass even though its rank (5) does not
    // match its own display position (6).
    const rows: RankInput[] = [
      { slug: 'p0', rank: 1 },
      { slug: 'p1', rank: 2 },
      { slug: 'p2', rank: null },
      { slug: 'p3', rank: 4 },
      { slug: 'p4', rank: 5 },
      { slug: 'x', rank: 5 },
      { slug: 'y', rank: null },
    ]
    const patches = swapRanks(rows, 6, 'up')
    expect(patches).toEqual(expect.arrayContaining([
      { slug: 'p2', priorityRank: 3 },
      { slug: 'y', priorityRank: 5 },
      { slug: 'x', priorityRank: 7 },
    ]))
    expect(patches).toHaveLength(3)
  })

  it('five unranked rows: moving index 3 up past index 2 normalises the whole list and leaves final ranks [1,2,4,3,5] by original index', () => {
    // Every deployment starts fully unranked — a plain pairwise swap would
    // hand two rows real rank numbers while the other three stay null, and
    // null sorts AFTER every real rank regardless of its value, so the moved
    // pair would jump to the FRONT of the list instead of trading places
    // in the middle. Normalising the whole visible order first is what keeps
    // the swap local.
    const rows: RankInput[] = Array.from({ length: 5 }, (_, i) => ({ slug: `p${i}`, rank: null }))
    const patches = swapRanks(rows, 3, 'up')
    expect(patches).not.toBeNull()
    expect(applyAndOrder(rows, patches!)).toEqual(['p0', 'p1', 'p3', 'p2', 'p4'])
    // Final rank per ORIGINAL index: p0->1, p1->2, p2->4, p3->3, p4->5.
    const finalRank = new Map(rows.map((r) => [r.slug, r.rank] as const))
    for (const p of patches!) finalRank.set(p.slug, p.priorityRank)
    expect([0, 1, 2, 3, 4].map((i) => finalRank.get(`p${i}`))).toEqual([1, 2, 4, 3, 5])
  })

  it('falls back to 1-based position when both sides are unranked, and normalises the remaining unranked row too', () => {
    const rows: RankInput[] = [
      { slug: 'a', rank: null },
      { slug: 'b', rank: null },
      { slug: 'c', rank: null },
    ]
    // Moving row index 2 ('c') up past index 1 ('b'): 'c' takes 'b's
    // position (2), 'b' takes 'c's old position (3) — a real swap even
    // though neither side ever had a stored rank. 'a' (index 0) is also
    // unranked and outside the pair, so it gets its own normalisation patch
    // to its displayed position (1) rather than being left null.
    expect(swapRanks(rows, 2, 'up')).toEqual([
      { slug: 'a', priorityRank: 1 },
      { slug: 'c', priorityRank: 2 },
      { slug: 'b', priorityRank: 3 },
    ])
  })

  it('out-of-range index yields null', () => {
    expect(swapRanks(products, -1, 'up')).toBeNull()
    expect(swapRanks(products, 3, 'down')).toBeNull()
  })
})
