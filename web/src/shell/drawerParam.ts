import { useSearchParams } from 'react-router-dom'

/**
 * The `?drawer=` contract, in one place.
 *
 * Drawers are URL state, not local state, so they survive a reload and a share.
 * Three regions open them (`NavRail`'s bottom cluster via `GlobalActions`, the
 * phone `TopBar`, and the `NavBar` More sheet), and a second copy of "build a
 * URLSearchParams, set `drawer`" is exactly how the param name and the drawer
 * that reads it drift apart. The name lives here; callers pass a `DrawerName`
 * and never touch the query string.
 */
export const DRAWER_PARAM = 'drawer'

export type DrawerName = 'assistant' | 'help'

export function useOpenDrawer(): (name: DrawerName) => void {
  const [searchParams, setSearchParams] = useSearchParams()
  return (name: DrawerName) => {
    const next = new URLSearchParams(searchParams)
    next.set(DRAWER_PARAM, name)
    setSearchParams(next)
  }
}
