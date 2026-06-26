/** Which edge action a settled swipe targets. */
export type SwipeSide = 'left' | 'right'

/** Resting state a released swipe resolves to. */
export type SwipeOutcome = 'closed' | 'open' | 'commit'

/**
 * One axis "wins" only when it leads the other by more than `bias` px, so a
 * near-diagonal drag stays ambiguous (caller keeps waiting) instead of
 * flickering between scroll and swipe. Reused for the vertical test by swapping
 * the arguments: `shouldLockHorizontal(dy, dx)`.
 */
export function shouldLockHorizontal(dx: number, dy: number, bias = 8): boolean {
  return Math.abs(dx) > Math.abs(dy) + bias
}

export interface SettleInput {
  /** Signed horizontal offset at release; negative = swiped left. */
  dx: number
  /** Card width in px. 0 when unknown (e.g. unmeasured in jsdom). */
  width: number
  /** Distance to snap the card open (reveal the button). */
  revealPx: number
  /** Fraction of card width past which a release commits directly. */
  commitFraction: number
}

/**
 * Resolve a released drag to a resting outcome + side. A zero width disables
 * direct commit (we never fire a destructive action off an unmeasured card).
 */
export function settleSwipe({ dx, width, revealPx, commitFraction }: SettleInput): {
  outcome: SwipeOutcome
  side: SwipeSide | null
} {
  const side: SwipeSide | null = dx < 0 ? 'left' : dx > 0 ? 'right' : null
  if (!side) return { outcome: 'closed', side: null }
  const dist = Math.abs(dx)
  if (width > 0 && dist >= width * commitFraction) return { outcome: 'commit', side }
  if (dist >= revealPx) return { outcome: 'open', side }
  return { outcome: 'closed', side: null }
}
