import * as React from 'react'
import { Outlet, useLocation } from 'react-router-dom'
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
 * `:id` alone is the wrong question. `/review/:id` is an ARCHIVED transcript and
 * must not attach a live runtime, and any future detail route (an initiative, a
 * product) would silently do the same. The section comes from `navDefForPath`,
 * the same map the route table is built from, so there is no second list of
 * "paths that own a session runtime" to drift. Task 12 moved that derivation to
 * `shell/openSessionId` so the dock branches on the identical value.
 *
 * ## Landmark
 *
 * This is a `<div>`, not a `<main>`. The pages still render their own `<main>`
 * (`ReviewView.tsx:108`, `SubSessionView.tsx:58`, and `App.tsx:55` until Task 17),
 * so making the region a landmark today would nest two `main`s. Ownership of the
 * single `main` landmark moves here when the pages stop declaring their own.
 *
 * ## `useNeedsYouTitle`
 *
 * Moved up from `Dashboard.tsx` so the needs-you count reaches every layout
 * mode, not just the phone dashboard. Since Task 2 it CONTRIBUTES a count to
 * `shell/documentTitle.ts` rather than writing `document.title`, so hoisting it
 * to a permanently-mounted region cannot clobber a page's own title.
 */
export function PageHost({ dock }: PageHostProps) {
  const { state } = useLocation()
  const { data: sessions = [] } = useSessions()
  useNeedsYouTitle(sessions)

  // Shared with `PromptDock` (see `shell/openSessionId`): the provider gate and
  // the dock's composer/dispatch branch must be the SAME answer, or the dock
  // renders `SessionComposer` outside the provider and throws.
  const openSessionId = useOpenSessionId()

  const body = (
    <div data-region="app.main" className="flex-1 min-h-0 flex flex-col">
      <Outlet />
      {dock}
    </div>
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
