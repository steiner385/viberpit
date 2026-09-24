import { Suspense, lazy, type ComponentProps } from 'react'
import type { BrowserTab as BrowserTabType } from './BrowserTab'

/**
 * `BrowserTab`, loaded when something actually shows it (#1411).
 *
 * The viewer is reachable from two places and neither is on a load path: a
 * session's Browser TAB, which the user has to switch to, and the account reauth
 * panel, which only exists mid-reauth. Imported statically it sat in the
 * preloaded set of every page load — including a phone opening the sessions
 * list, which has no browser tab at all.
 *
 * This is the boundary, not `BrowserTab.tsx` itself, so the component keeps a
 * plain synchronous export for its own tests and for anything that legitimately
 * wants it eagerly. Import `BrowserTab` directly from a render path and the
 * weight comes straight back — `npm run budget` is what notices.
 */
const BrowserTab = lazy(() => import('./BrowserTab').then((m) => ({ default: m.BrowserTab })))

export function DeferredBrowserTab(props: ComponentProps<typeof BrowserTabType>) {
  return (
    // The viewer paints into a canvas that is black until the framebuffer
    // arrives, so an empty box is what the next frame looks like anyway —
    // a spinner here would be a flash of something the connect immediately
    // replaces.
    <Suspense fallback={<div className="h-full w-full" aria-busy="true" />}>
      <BrowserTab {...props} />
    </Suspense>
  )
}
