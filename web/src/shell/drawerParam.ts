import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

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
 * Both writers use `replace` — umbrella §4.4, "overlay params use
 * `setSearchParams(…, { replace: true })`". Opening a drawer is not a place in
 * the app you can be sent Back to; with `push`, Back out of a drawer would step
 * through every open/close of it before it left the page.
 */
export const DRAWER_PARAM = 'drawer'

/** Every drawer `DrawerHost` can render. `DrawerName` is derived from it, so the
 *  runtime validator and the type cannot disagree about what is a drawer. */
export const DRAWER_NAMES = ['assistant', 'help'] as const

export type DrawerName = (typeof DRAWER_NAMES)[number]

export function isDrawerName(value: string | null | undefined): value is DrawerName {
  return value !== null && value !== undefined && (DRAWER_NAMES as readonly string[]).includes(value)
}

/**
 * The open drawer, or `null`.
 *
 * An unrecognised value reads as `null` rather than as "some drawer": a
 * `?drawer=bogus` from a stale link or a typo must render nothing, not an empty
 * sheet the user has to dismiss.
 */
export function useDrawerName(): DrawerName | null {
  const [searchParams] = useSearchParams()
  const raw = searchParams.get(DRAWER_PARAM)
  return isDrawerName(raw) ? raw : null
}

export function useOpenDrawer(): (name: DrawerName) => void {
  const [searchParams, setSearchParams] = useSearchParams()
  return useCallback(
    (name: DrawerName) => {
      const next = new URLSearchParams(searchParams)
      next.set(DRAWER_PARAM, name)
      setSearchParams(next, { replace: true })
    },
    [searchParams, setSearchParams],
  )
}

/**
 * `to` with the open drawer carried over — the pure half of the rule below.
 *
 * Merges rather than appends: `GlobalActions` already ships a nav target that
 * carries a query string (`/sessions?dialog=new`), so a naive `` `${to}?drawer=` ``
 * would produce two `?` and a path nothing matches.
 */
export function withDrawer(to: string, drawer: DrawerName): string {
  const [beforeHash, hash] = to.split('#')
  const [path, query = ''] = beforeHash.split('?')
  const params = new URLSearchParams(query)
  params.set(DRAWER_PARAM, drawer)
  return `${path}?${params}${hash === undefined ? '' : `#${hash}`}`
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
  const [searchParams, setSearchParams] = useSearchParams()
  return useCallback(() => {
    const next = new URLSearchParams(searchParams)
    next.delete(DRAWER_PARAM)
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])
}
