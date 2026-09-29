import { useEffect, useState } from 'react'
import { setBaseTitle, releaseBaseTitle } from '@/shell/documentTitle'

/**
 * Claim the page's base title in `shell/documentTitle.ts` for as long as the
 * caller is mounted — the effect pair `ui/PageHeader` used to carry inline,
 * extracted so a page WITHOUT a `PageHeader` (the Sessions pane, whose header
 * is `SessionHeader`; the empty panes) can meet the page contract's
 * `document.title` rule through the same code rather than a second copy.
 *
 * A stable per-instance token (useState's initializer, not a ref — the
 * react-hooks/refs rule rejects reading a ref during render) identifies this
 * caller's slot on the base-title stack. Claim/update runs on every title
 * change; release runs exactly once, on unmount. They are split so a title
 * change does not release-then-reclaim the slot — that would pop it off the
 * stack and push it back on top, reordering it ahead of a page mounted after
 * it while both are visible during a route transition.
 */
export function usePageTitle(base: string): void {
  const [token] = useState(() => Symbol('page-title'))
  useEffect(() => {
    setBaseTitle(token, base)
  }, [token, base])
  useEffect(() => {
    return () => releaseBaseTitle(token)
  }, [token])
}
