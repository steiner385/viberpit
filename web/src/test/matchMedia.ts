// The layout `matchMedia` stub, in one place.
//
// Four test files had byte-identical copies of it (`useViewport`, `NavRail`,
// `ContextList`, `DrawerHost`), each with a comment pointing at one of the
// others as the original. `AppShell.test.tsx` would have been the fifth, and a
// fifth copy is how one of them quietly stops reporting `addEventListener` (or
// gains a `matches` default) and starts testing a different browser from its
// siblings. The queries themselves live here too, so a breakpoint that moves in
// `useViewport.tsx` is edited in exactly two places rather than six.
//
// Non-layout stubs are deliberately NOT routed here: `WorkingIndicator.test.tsx`
// stubs `prefers-reduced-motion` with a different signature (a boolean, not a
// query list) and installs it with `Object.defineProperty` rather than
// `vi.stubGlobal`. Folding it in would change how that file's stub is torn down,
// which is a different concern from the one this module owns.
import { vi } from 'vitest'
import type { WidthTier } from '@/shell/useViewport'

/**
 * Every layout query `useViewport` reads. Named rather than inlined so a test
 * cannot half-match a breakpoint (`min-width:768px` with no space matches
 * nothing and silently reports phone).
 */
export const MEDIA = {
  tablet: '(min-width: 768px)',
  desktop: '(min-width: 1024px)',
  /** D13's drawer-push threshold — deliberately between the width tiers. */
  drawerPush: '(min-width: 1280px)',
  wide: '(min-width: 1440px)',
  coarsePointer: '(pointer: coarse)',
} as const

/**
 * The queries that report true at each width tier.
 *
 * `wide` includes `drawerPush`: 1440 is inside 1280, so a stub that omitted it
 * would describe a viewport no real browser can produce.
 */
export const WIDTH_TIER_QUERIES: Record<WidthTier, readonly string[]> = {
  phone: [],
  tablet: [MEDIA.tablet],
  desktop: [MEDIA.tablet, MEDIA.desktop],
  wide: [MEDIA.tablet, MEDIA.desktop, MEDIA.drawerPush, MEDIA.wide],
}

/** Stub `matchMedia` so that every query in `matching` reports true. */
export function stubMatchMedia(matching: readonly string[]): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: matching.includes(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}
