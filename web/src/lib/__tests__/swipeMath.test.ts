import { describe, it, expect } from 'vitest'
import { shouldLockHorizontal, settleSwipe } from '../swipeMath'

describe('shouldLockHorizontal', () => {
  it('locks when horizontal leads vertical by more than the bias', () => {
    expect(shouldLockHorizontal(20, 0)).toBe(true)
    expect(shouldLockHorizontal(-20, 3)).toBe(true)
  })
  it('does not lock for a vertical-dominant or ambiguous move', () => {
    expect(shouldLockHorizontal(2, 30)).toBe(false) // clearly vertical
    expect(shouldLockHorizontal(5, 0)).toBe(false)  // within default bias (8)
  })
})

describe('settleSwipe', () => {
  const base = { width: 300, revealPx: 64, commitFraction: 0.45 } // commit at 135px

  it('commits left when dragged left past the commit fraction', () => {
    expect(settleSwipe({ dx: -140, ...base })).toEqual({ outcome: 'commit', side: 'left' })
  })
  it('commits right when dragged right past the commit fraction', () => {
    expect(settleSwipe({ dx: 150, ...base })).toEqual({ outcome: 'commit', side: 'right' })
  })
  it('opens (reveals) between the reveal and commit thresholds', () => {
    expect(settleSwipe({ dx: -80, ...base })).toEqual({ outcome: 'open', side: 'left' })
    expect(settleSwipe({ dx: 80, ...base })).toEqual({ outcome: 'open', side: 'right' })
  })
  it('closes below the reveal threshold', () => {
    expect(settleSwipe({ dx: -40, ...base })).toEqual({ outcome: 'closed', side: null })
    expect(settleSwipe({ dx: 0, ...base })).toEqual({ outcome: 'closed', side: null })
  })
  it('never commits when width is unknown (0) — only reveal/close', () => {
    expect(settleSwipe({ dx: -500, width: 0, revealPx: 64, commitFraction: 0.45 }))
      .toEqual({ outcome: 'open', side: 'left' })
  })
})
