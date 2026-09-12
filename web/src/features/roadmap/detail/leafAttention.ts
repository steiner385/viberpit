import type { Leaf } from '@/api/roadmap'

/** Match the issue list's open-state semantics; completed issues need no action. */
export function leafNeedsAttention(leaf: Leaf): boolean {
  return leaf.state === 'open' && (leaf.tier === 'needs-you' || leaf.tier === 'blocked')
}
