import type { Leaf } from '@/api/roadmap'

/** Match the issue list's open-state semantics; completed issues need no action.
 *  An open needs-you or blocked leaf gets its "what clears this" guidance. */
export function leafNeedsAttention(leaf: Leaf): boolean {
  return leaf.state === 'open' && (leaf.tier === 'needs-you' || leaf.tier === 'blocked')
}

/** The Needs-you population at leaf level (design 2026-10-14 §1, the same rule
 *  as `needsYou.ts`): open and only a human can unblock it. A blocked leaf is
 *  waiting on other work, so the attention-only list leaves it out. */
export function leafNeedsYou(leaf: Leaf): boolean {
  return leaf.state === 'open' && leaf.tier === 'needs-you'
}
