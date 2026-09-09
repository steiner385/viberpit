// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { riceScore } from './rice'

describe('riceScore mirrors server/pm/score.py', () => {
  it('computes reach*impact*confidence/effort to 2 dp', () => {
    expect(riceScore(3, 3, 2, 80)).toBe(360)
    expect(riceScore(1, 1, 3, 50)).toBe(16.67)
  })

  it('is null when any input is missing or effort is 0', () => {
    expect(riceScore(null, 3, 2, 80)).toBeNull()
    expect(riceScore(3, null, 2, 80)).toBeNull()
    expect(riceScore(3, 3, null, 80)).toBeNull()
    expect(riceScore(3, 3, 2, null)).toBeNull()
    expect(riceScore(3, 3, 0, 80)).toBeNull()
    expect(riceScore(3, 3, undefined, 80)).toBeNull()
  })

  // The server returns `None` for a zero effort but happily divides by a
  // negative one; a 0 in any of the other three is a real score of 0, not
  // "unscored" (`RICE_MIN` allows it), and the read-out must say so rather
  // than falling back to the em dash.
  it('keeps a genuine zero, which is not the same as unscored', () => {
    expect(riceScore(0, 3, 2, 80)).toBe(0)
  })
})
