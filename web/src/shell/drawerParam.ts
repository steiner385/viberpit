import { useCallback } from 'react'
import {
  isOverlayName, useClearOverlayParam, useOverlayParam, useSetOverlayParam, withOverlayParam,
} from './overlayParam'

/**
 * The `?drawer=` contract, in one place.
 *
 * Drawers are URL state, not local state, so they survive a reload and a share.
 * Three regions open them (`NavRail`'s bottom cluster via `GlobalActions`, the
 * phone `TopBar`, and the `NavBar` More sheet), one region reads them
 * (`DrawerHost`), and a second copy of "build a URLSearchParams, set `drawer`"
 * is exactly how the param name and the drawer that reads it drift apart. The
 * name lives here; callers pass a `DrawerName` and never touch the query string.
 *
 * The MECHANICS — allow-listed read, `replace` write — moved to
 * `overlayParam.ts` in Task 18, when `?dialog=` needed the same three hooks.
 * What stays here is this param's own vocabulary and the one rule that is
 * genuinely drawer-specific: an open drawer survives a section change.
 */
export const DRAWER_PARAM = 'drawer'

/** Every drawer `DrawerHost` can render. `DrawerName` is derived from it, so the
 *  runtime validator and the type cannot disagree about what is a drawer. */
export const DRAWER_NAMES = ['assistant', 'help'] as const

export type DrawerName = (typeof DRAWER_NAMES)[number]

export function isDrawerName(value: string | null | undefined): value is DrawerName {
  return isOverlayName(value, DRAWER_NAMES)
}

/**
 * The open drawer, or `null`.
 *
 * An unrecognised value reads as `null` rather than as "some drawer": a
 * `?drawer=bogus` from a stale link or a typo must render nothing, not an empty
 * sheet the user has to dismiss.
 */
export function useDrawerName(): DrawerName | null {
  return useOverlayParam(DRAWER_PARAM, DRAWER_NAMES)
}

export function useOpenDrawer(): (name: DrawerName) => void {
  return useSetOverlayParam<DrawerName>(DRAWER_PARAM)
}

/** `to` with the open drawer carried over — the pure half of the rule below. */
export function withDrawer(to: string, drawer: DrawerName): string {
  return withOverlayParam(to, DRAWER_PARAM, drawer)
}

/**
 * Rewrites a nav target so an open drawer survives a section change
 * (sub-spec B §4.2: "the drawer element stays mounted while `?drawer=` is set
 * across route changes").
 *
 * It lives here, next to the param it preserves, and is applied by `NavItem` —
 * the one component that turns a nav target into a `<Link>`. The alternative,
 * intercepting navigation in `AppShell`, would mean the shell re-deciding a
 * destination the nav item had already built, and would leave the rendered
 * `href` disagreeing with where a click actually lands: middle-click and
 * "copy link address" would silently drop the drawer.
 *
 * ## Scope: NAV targets only, deliberately (S4)
 *
 * This applies to the rail and bar links and to nothing else. Every other
 * navigation — clicking a session row, the archive-advance, `MainPaneSession`'s
 * Back and prev/next chevrons, opening a just-created session from the dock —
 * drops `?drawer=` and therefore closes the drawer. That asymmetry is a
 * decision, not an oversight, and it is pinned by a test beside the preserving
 * one (`AppShell.test.tsx`, "an open drawer survives a section change").
 *
 * The reasons, in order:
 *
 * - **The rule is about sections.** Switching SECTION with a drawer docked open
 *   is the case §4.2 names, and it is the one where closing the drawer would be
 *   surprising: nothing about the drawer changed, only what is beside it.
 *   Opening an ITEM is a different gesture — the user asked for a session, and
 *   giving the main pane its width back is a reasonable answer to that.
 * - **"All navigation alike" means all of it.** There are thirteen programmatic
 *   `navigate()` calls in `shell/` and `features/` alone, plus `<Link>`s inside
 *   pages (`pipeline/PrRow`'s session link). A `useDrawerPreservingNavigate()`
 *   applied to some of them would replace a visible inconsistency with a subtler
 *   one, which is the shape this repo's rules exist to prevent.
 * - **Below 1280 the question does not arise.** The drawer is a focus-trapping
 *   overlay there, so the only navigations reachable while it is open are the
 *   ones it renders itself. This is a push-mode (≥1280) phenomenon.
 *
 * When it should change: the Assistant drawer (PR 12) is the one whose whole
 * value is following you across the app. That is the PR that should introduce a
 * single navigation helper and route every call site through it — with the
 * drawer's own `persist` flag deciding, rather than the caller.
 */
export function useDrawerPreservingTo(): (to: string) => string {
  const drawer = useDrawerName()
  return useCallback((to: string) => (drawer ? withDrawer(to, drawer) : to), [drawer])
}

export function useCloseDrawer(): () => void {
  return useClearOverlayParam(DRAWER_PARAM)
}
