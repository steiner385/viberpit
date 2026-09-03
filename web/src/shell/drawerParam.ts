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
 */
export function useDrawerPreservingTo(): (to: string) => string {
  const drawer = useDrawerName()
  return useCallback((to: string) => (drawer ? withDrawer(to, drawer) : to), [drawer])
}

export function useCloseDrawer(): () => void {
  return useClearOverlayParam(DRAWER_PARAM)
}
