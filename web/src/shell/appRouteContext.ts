import { createContext, useContext } from 'react'

/**
 * Whether a pathname is a page of this app — `isAppRoute` from `./routes`,
 * provided once by `App`. A context rather than an import because its reader
 * (the lazily loaded transcript markdown renderer) must not pull the route
 * table — every page — into its own chunk graph; `npm run budget` says so.
 * Null outside the app (a test with no provider): nothing is a page.
 */
export const AppRouteContext = createContext<((pathname: string) => boolean) | null>(null)

export function useIsAppRoute(): (pathname: string) => boolean {
  return useContext(AppRouteContext) ?? (() => false)
}
