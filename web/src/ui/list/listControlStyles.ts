import { cn } from '@/lib/utils'

/** The compact control chrome every list-controls strip shares — the group/sort
 *  selects and the search box (`ListSearchInput`). One definition, because the
 *  search box now lives in its own module and a second copy of this string is
 *  how the two would drift apart.
 *
 *  `min-h-11` (the `--target` token's 44px, `docs/ACCESSIBILITY.md` §4) floors
 *  the touch target on a coarse pointer; `[@media(hover:hover)]:min-h-0` lifts
 *  the floor back off for a hover-capable (desktop mouse) pointer, so the
 *  dense `py-1` chrome is unchanged there — the same touch/hover split
 *  `CardMenu`'s `actionBtn` already uses for the per-row `⋯` trigger. */
export const listControlClass = cn(
  'text-xs rounded-md border border-edge',
  'bg-surface text-ink',
  'min-h-11 [@media(hover:hover)]:min-h-0',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
  'disabled:opacity-60',
)
