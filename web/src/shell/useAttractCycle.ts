/**
 * Kiosk attract cycle (shell spec §5, "PR 10 completion"): a wall display
 * launched with `?kiosk=1&pages=a,b&cycle=N` rotates through those SECTIONS
 * every N seconds. This is the page-level rotation; the Pipeline page's own
 * `useRepoCycle` rotates repos WITHIN its page on the same cadence.
 *
 * Paused while `document.hidden` (a hidden tab skips ticks rather than
 * navigating blind), navigates with `replace` (the cycle is not history) and
 * keeps the search string (the kiosk params ARE the display's configuration).
 * One page is nothing to rotate, so the default `pages=['pipeline']` is a no-op.
 */
import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { NAV_BY_ID, navDefForPath, type NavId } from './navDefs'

export function useAttractCycle(enabled: boolean, pages: readonly NavId[], cycleSeconds: number): void {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  useEffect(() => {
    if (!enabled || pages.length < 2) return
    const id = setInterval(() => {
      if (document.hidden) return
      const current = navDefForPath(pathname)?.id
      const i = current ? pages.indexOf(current) : -1
      const next = pages[(i + 1) % pages.length]!
      navigate({ pathname: NAV_BY_ID[next].path, search }, { replace: true })
    }, cycleSeconds * 1000)
    return () => clearInterval(id)
  }, [enabled, pages, cycleSeconds, pathname, search, navigate])
}
