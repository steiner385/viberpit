import { describe, it, expect, vi } from 'vitest'
import { countTiles, humanizeKey, MAX_COUNT_TILES } from './countTiles'

describe('countTiles', () => {
  it('humanizes snake_case keys into a capitalized phrase', () => {
    expect(humanizeKey('needs_human')).toBe('Needs human')
    expect(humanizeKey('gh_projects')).toBe('Gh projects')
    expect(humanizeKey('failing')).toBe('Failing')
  })
  // 2026-10-01 Delivery critique, P1: six tiles were three phone rows that
  // pushed the strip below the fold. Four is two rows; the queue leads.
  it('leads with a clickable, toned Needs you tile and keeps three server tiles behind it, in server order', () => {
    // Review focus 3: gh-reconcile publishes eight keys.
    const counts = { products: 6, planned: 1, applied: 1, failed: 0, parked: 0, skipped_stale: 2, halted_cap: 0, dry_run: 0 }
    const onClick = vi.fn()
    const tiles = countTiles(counts, { value: 2, onClick })
    expect(tiles).toHaveLength(MAX_COUNT_TILES)
    expect(tiles.map((t) => t.label)).toEqual(['Needs you', 'Products', 'Planned', 'Applied'])
    expect(tiles[0]).toMatchObject({ label: 'Needs you', value: 2, tone: 'warning' })
    tiles[0]!.onClick!()
    expect(onClick).toHaveBeenCalledTimes(1)
    // The server tiles stay untoned and unclickable: they map to no tab.
    expect(tiles[1]).toEqual({ label: 'Products', value: 6 })
  })
  // #2936: a paused automation's decisions are parked — named, not amber.
  it('a parked lead says Parked and is untoned, still opening the tab', () => {
    const onClick = vi.fn()
    const [lead] = countTiles({ failing: 1 }, { value: 50, onClick, parked: true })
    expect(lead).toEqual({ label: 'Parked', value: 50, onClick })
  })
  it('omits the lead while the actionable count is unknown, without letting a fourth server tile take its place', () => {
    const counts = { a: 1, b: 2, c: 3, d: 4 }
    expect(countTiles(counts).map((t) => t.label)).toEqual(['A', 'B', 'C'])
    expect(countTiles(counts, { value: 0, onClick: () => {} }).map((t) => t.label)).toEqual(['Needs you', 'A', 'B', 'C'])
  })
  it('skips non-finite values and answers nothing for null or empty counts', () => {
    // Review focus 4.
    expect(countTiles({ ok: 1, bad: null as unknown as number, nan: Number.NaN })).toEqual([{ label: 'Ok', value: 1 }])
    expect(countTiles(null)).toEqual([])
    expect(countTiles(undefined)).toEqual([])
    expect(countTiles({})).toEqual([])
    // No server counts means no band at all, even with a known Needs you count:
    // the tab badge carries it there.
    expect(countTiles(null, { value: 3, onClick: () => {} })).toEqual([])
  })
})
