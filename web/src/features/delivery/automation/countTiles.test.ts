import { describe, it, expect } from 'vitest'
import { countTiles, humanizeKey, MAX_COUNT_TILES } from './countTiles'

describe('countTiles', () => {
  it('humanizes snake_case keys into a capitalized phrase', () => {
    expect(humanizeKey('needs_human')).toBe('Needs human')
    expect(humanizeKey('gh_projects')).toBe('Gh projects')
    expect(humanizeKey('failing')).toBe('Failing')
  })
  it('keeps the server order and caps at six tiles', () => {
    // Review focus 3: gh-reconcile publishes eight keys.
    const counts = { products: 6, planned: 1, applied: 1, failed: 0, parked: 0, skipped_stale: 2, halted_cap: 0, dry_run: 0 }
    const tiles = countTiles(counts)
    expect(tiles).toHaveLength(MAX_COUNT_TILES)
    expect(tiles.map((t) => t.label)).toEqual(['Products', 'Planned', 'Applied', 'Failed', 'Parked', 'Skipped stale'])
    expect(tiles[0]).toEqual({ label: 'Products', value: 6 })
  })
  it('skips non-finite values and answers nothing for null or empty counts', () => {
    // Review focus 4.
    expect(countTiles({ ok: 1, bad: null as unknown as number, nan: Number.NaN })).toEqual([{ label: 'Ok', value: 1 }])
    expect(countTiles(null)).toEqual([])
    expect(countTiles(undefined)).toEqual([])
    expect(countTiles({})).toEqual([])
  })
})
