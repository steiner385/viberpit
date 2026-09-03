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

export function useCloseDrawer(): () => void {
  const [searchParams, setSearchParams] = useSearchParams()
  return useCallback(() => {
    const next = new URLSearchParams(searchParams)
    next.delete(DRAWER_PARAM)
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])
}
