import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { storageKey } from '@/lib/storageKey'
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
 * Drawers that stay open until the user closes them — across EVERY navigation,
 * and across a reload.
 *
 * The URL alone cannot hold that promise: most navigations (opening a session,
 * Back, the archive-advance, a `<Link>` inside a page) build a fresh `to` and
 * drop `?drawer=`, which is the S4 rule below and is right for Help. The
 * Assistant's whole value is following you across the app, so its open state
 * also lives in a persisted store that only an explicit close clears. Reading
 * it as a fallback in `useDrawerName` — rather than re-adding `?drawer=` after
 * each navigation — means the drawer never unmounts for a frame, so the
 * transcript scroll and the composer's draft survive the navigation too.
 */
export const STICKY_DRAWERS: readonly DrawerName[] = ['assistant']
export const STICKY_DRAWER_KEY = storageKey('stickyDrawer')

function isStickyDrawer(name: DrawerName | null): name is DrawerName {
  return name !== null && STICKY_DRAWERS.includes(name)
}

export function readStickyDrawer(): DrawerName | null {
  try {
    const raw = localStorage.getItem(STICKY_DRAWER_KEY)
    return isDrawerName(raw) && isStickyDrawer(raw) ? raw : null
  } catch { return null }
}

const stickyListeners = new Set<() => void>()

function writeStickyDrawer(name: DrawerName | null): void {
  try {
    if (name) localStorage.setItem(STICKY_DRAWER_KEY, name)
    else localStorage.removeItem(STICKY_DRAWER_KEY)
  } catch { /* storage unavailable — the URL still carries the drawer */ }
  stickyListeners.forEach((l) => l())
}

function subscribeSticky(cb: () => void) {
  stickyListeners.add(cb)
  return () => { stickyListeners.delete(cb) }
}

function useStickyDrawer(): DrawerName | null {
  return useSyncExternalStore(subscribeSticky, readStickyDrawer, () => null)
}

/**
 * The open drawer, or `null`: `?drawer=` when it names one, else the sticky
 * drawer the user has not closed.
 *
 * An unrecognised value reads as `null` rather than as "some drawer": a
 * `?drawer=bogus` from a stale link or a typo must render nothing, not an empty
 * sheet the user has to dismiss.
 */
export function useDrawerName(): DrawerName | null {
  const fromUrl = useOverlayParam(DRAWER_PARAM, DRAWER_NAMES)
  const sticky = useStickyDrawer()
  // A shared `?drawer=assistant` link pins the drawer just as the button does,
  // so the first navigation away from the link does not close it.
  useEffect(() => {
    if (isStickyDrawer(fromUrl) && sticky !== fromUrl) writeStickyDrawer(fromUrl)
  }, [fromUrl, sticky])
  return fromUrl ?? sticky
}

export function useOpenDrawer(): (name: DrawerName) => void {
  const setParam = useSetOverlayParam<DrawerName>(DRAWER_PARAM)
  return useCallback((name: DrawerName) => {
    // Opening Help over a pinned Assistant leaves the pin alone: closing Help
    // then lands back on the Assistant the user never closed.
    if (isStickyDrawer(name)) writeStickyDrawer(name)
    setParam(name)
  }, [setParam])
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
 * drops `?drawer=` and therefore closes a non-sticky drawer. That asymmetry is a
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
 * The Assistant is the exception, and it is decided by the drawer, not the
 * caller: it is in `STICKY_DRAWERS`, so it survives every navigation without a
 * single call site changing. This hook therefore carries only the URL's own
 * `?drawer=` — a pinned Assistant needs no help, and adding it to every nav
 * `href` would just leave `?drawer=assistant` in history entries for Back to
 * resurrect after the user closed it.
 */
export function useDrawerPreservingTo(): (to: string) => string {
  const drawer = useOverlayParam(DRAWER_PARAM, DRAWER_NAMES)
  return useCallback((to: string) => (drawer ? withDrawer(to, drawer) : to), [drawer])
}

/**
 * Closes the drawer on screen — the only thing that unpins a sticky one. With
 * Help open over a pinned Assistant, it closes Help and the Assistant remains.
 */
export function useCloseDrawer(): () => void {
  const clearParam = useClearOverlayParam(DRAWER_PARAM)
  const fromUrl = useOverlayParam(DRAWER_PARAM, DRAWER_NAMES)
  return useCallback(() => {
    if (fromUrl === null || isStickyDrawer(fromUrl)) writeStickyDrawer(null)
    clearParam()
  }, [clearParam, fromUrl])
}
