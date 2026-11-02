import { cn } from '@/lib/utils'

/** The 44px touch-target floor (`--target`, docs/ACCESSIBILITY.md §4) for a
 *  dense control, lifted back off where `hover: hover` matches so a desktop
 *  mouse keeps the compact size. The one copy of the split — `listControlClass`,
 *  `CardMenu`'s menu rows and the D-5 review buttons all route through it. */
export const TOUCH_FLOOR = 'min-h-11 [@media(hover:hover)]:min-h-0'

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
  TOUCH_FLOOR,
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
  'disabled:opacity-60',
)
