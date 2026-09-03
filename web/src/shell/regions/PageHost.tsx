import * as React from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useSessions } from '@/api/hooks'
import { useNeedsYouTitle } from '@/hooks/useNeedsYouTitle'
import { SessionRuntimeProvider } from '@/components/session/SessionRuntimeProvider'
import { useOpenSessionId } from '@/shell/openSessionId'
import { readInitialPrompt } from '@/session/sessionNavState'

export interface PageHostProps {
  /**
   * The composer, handed in by `AppShell` so it lands INSIDE the session
   * runtime provider and reads the same runtime the transcript does.
   *
   * It is a prop rather than an import because whether a dock exists at all is
   * a shell decision (`app.dock` is Sessions-only, DEC-10). `PageHost` must not
   * grow a second opinion about that: two owners of "is the composer visible"
   * is how the region and the shell come to disagree.
   */
  dock?: React.ReactNode
  /**
   * Hide the PAGE — not the region, and never the dock.
   *
   * Phone and tablet give the one content band to EITHER the list or the page
   * (sub-spec B §4.1, "list | main"), and `AppShell` decides which. It cannot
   * express that by leaving `app.main` unrendered: the matrix has `app.main`
   * present in every mode, `useNeedsYouTitle` and the session runtime live here,
   * and — the half that bites — the dock is INSIDE this region, so hiding the
   * region would take the phone's dispatch bar with it. On `/sessions` with the
   * list up, the list and the prompt bar are exactly what ships today
   * (`Dashboard`), and the welcome pane behind them is what must not show.
   *
   * The page is hidden, not unmounted, so a mode change or a list open/close
   * does not remount the routed page and throw away its scroll and query state.
   */
  pageHidden?: boolean
}

/**
 * `app.main` — the routed page, plus the session runtime when one is open.
 *
 * ## Scope is the entire point (audit F2.3)
 *
 * The provider wraps the outlet and the dock, and nothing else.
 *
 * `DesktopShell` wrapped the WHOLE SHELL in `<SessionRuntimeProvider
 * key={selection.id}>` (`DesktopShell.tsx:673-677`) — header, tab bar, banners,
 * the session list, the resizer, the main pane, the prompt bar and all three
 * dialogs. Because the provider is keyed on the focused session id, selecting a
 * different session remounted every one of those, and opening or closing a
 * session mounted/unmounted the provider, remounting them both ways. The
 * `listScrollTop` + `restoreListScroll` callback-ref pair (`DesktopShell.tsx:186-195,
 * 493-494`) existed purely to restore the session list's scroll position after a
 * remount that should never have happened.
 *
 * With the provider scoped here, `ContextList` sits outside it and simply never
 * remounts — `ContextList.test.tsx` pins that by DOM-node identity, and this
 * file's tests pin the other side of the same boundary: a probe rendered as a
 * SIBLING of `PageHost` still throws out of `useSessionRuntimeContext()` on
 * `/sessions/:id`, so the provider demonstrably does not reach above this region.
 *
 * The remount that IS wanted stays: the provider is keyed on the session id, so
 * each session gets a fresh reducer and a clean attach/detach cycle. That reset
 * now stops at this region's own root instead of taking the shell with it.
 *
 * ## Why the runtime is gated on the section, not just on `:id`
 *
 * `:id` alone is the wrong question. `/archived/:id` is an ARCHIVED transcript and
 * must not attach a live runtime, and any future detail route (an initiative, a
 * product) would silently do the same. The section comes from `navDefForPath`,
 * the same map the route table is built from, so there is no second list of
 * "paths that own a session runtime" to drift. Task 12 moved that derivation to
 * `shell/openSessionId` so the dock branches on the identical value.
 *
 * ## Landmark
 *
 * This region IS the page's `<main>`. It could not be until Task 17: `App.tsx`,
 * `ReviewView` and `SubSessionView` each declared their own, so promoting the
 * region would have nested two. Task 17 deleted `App.tsx`'s shell fork and
 * demoted the other two to `<div>`s, which puts the single `main` landmark
 * here — one owner, present in every layout mode, wrapping exactly the routed
 * page and the dock. `AppShell.test.tsx`'s axe passes run with the `region`
 * rule ON as a result: "all page content is inside a landmark" is now true.
 *
 * It stays a landmark even while `pageHidden` is set — the dock inside it is
 * still on screen, and that is precisely the content the rule is about.
 *
 * ## `useNeedsYouTitle`
 *
 * Moved up from `Dashboard.tsx` so the needs-you count reaches every layout
 * mode, not just the phone dashboard. Since Task 2 it CONTRIBUTES a count to
 * `shell/documentTitle.ts` rather than writing `document.title`, so hoisting it
 * to a permanently-mounted region cannot clobber a page's own title.
 */
export function PageHost({ dock, pageHidden = false }: PageHostProps) {
  const { state } = useLocation()
  const { data: sessions = [] } = useSessions()
  useNeedsYouTitle(sessions)

  // Shared with `PromptDock` (see `shell/openSessionId`): the provider gate and
  // the dock's composer/dispatch branch must be the SAME answer, or the dock
  // renders `SessionComposer` outside the provider and throws.
  const openSessionId = useOpenSessionId()

  const body = (
    <main
      data-region="app.main"
      className={cn('min-h-0 flex flex-col', pageHidden ? 'shrink-0' : 'flex-1')}
    >
      {/* Always wrapped, so toggling `pageHidden` changes an attribute rather
          than the element tree — a conditional wrapper would remount the page.
          The `hidden` ATTRIBUTE, not a `hidden` class: it also takes the page
          out of the accessibility tree, so a screen reader on a phone is not
          walking a welcome pane that is not on screen. The layout classes are
          dropped while hidden because `display: flex` would override the UA's
          `[hidden] { display: none }`. */}
      <div hidden={pageHidden} className={pageHidden ? undefined : 'flex-1 min-h-0 flex flex-col'}>
        <Outlet />
      </div>
      {dock}
    </main>
  )

  return openSessionId ? (
    <SessionRuntimeProvider
      key={openSessionId}
      sessionId={openSessionId}
      initialPrompt={readInitialPrompt(state)}
    >
      {body}
    </SessionRuntimeProvider>
  ) : (
    body
  )
}
