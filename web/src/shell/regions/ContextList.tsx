import * as React from 'react'
import { useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { SidebarResizer } from '@/components/SidebarResizer'
import { useSidebarWidth } from '@/lib/useSidebarWidth'
import { navDefForPath } from '@/shell/navDefs'
import { useViewport } from '@/shell/useViewport'
import {
  SessionListColumn,
  type SessionListColumnProps,
} from '@/features/sessions/SessionListColumn'

export interface ContextListProps {
  /**
   * Props for the Sessions list column. Sessions is the only section with a
   * list today (`NAV_DEFS[…].hasList`); a section that grows one adds a sibling
   * field here and a branch below, and nothing else in the shell changes.
   *
   * The state handle comes from `useSessionListState()` ABOVE this region,
   * because the prompt bar drives the same cursor — see that hook's header.
   */
  sessions: SessionListColumnProps
}

/**
 * `app.list` — the section's list column.
 *
 * Whether a section has one is read from `navDefForPath(pathname)?.hasList`, so
 * there is no second list of "sections with a sidebar" to keep in sync. On a
 * section without one this renders `null` — the region node is absent, not an
 * empty shell that still eats a grid column.
 *
 * ## Why this is a region and not part of the page
 *
 * It renders on `/sessions` AND `/sessions/:id`, from a component that sits
 * outside the route tree, so a `:id` change re-renders it and never remounts it.
 * `DesktopShell` had it the other way round: the whole shell sat inside a
 * `SessionRuntimeProvider` keyed on the focused session id, so selecting a
 * session remounted the list and reset its scrollTop — which it papered over
 * with a `listScrollTop` ref plus a `restoreListScroll` callback ref
 * (`DesktopShell.tsx:185-195, 493-494`). Task 9's column carries no such
 * workaround, and `ContextList.test.tsx` pins the reason by DOM-node identity.
 *
 * ## Naming
 *
 * The landmark is named from the nav def's own label, via a visually-hidden
 * heading. Phone's `Dashboard` had a visible `<h2>Sessions</h2>`; `DesktopShell`
 * had a bare `<aside>` with no name at all. Task 9's column dropped the heading
 * and the `aria-label` it left behind sits on a role-less div, where assistive
 * tech ignores it — so `app.list` had no accessible name in either direction.
 * A heading (rather than an `aria-label`) restores phone's semantics and keeps
 * the surface reachable by heading navigation; it is `sr-only` because the wide
 * layout has never shown a visible title above the column and adding one is a
 * design change, not an accessibility fix. One string, from `NAV_DEFS`, so the
 * landmark cannot drift from the nav item that leads to it.
 *
 * ## Width
 *
 * Resizable at `desktop`/`wide` through the existing `SidebarResizer` +
 * `useSidebarWidth` (persisted under `ccswitch.listWidth`, DEC-11). Below that
 * the column fills whatever the shell gives it: a fixed px width on a phone is
 * either an overflow or a wasted band, and there is no room for a drag handle.
 * The resizer is also the divider, so no border is drawn where it is absent.
 */
export function ContextList({ sessions }: ContextListProps) {
  const { pathname } = useLocation()
  const { width: tier } = useViewport()
  const [listWidth, setListWidth] = useSidebarWidth()
  const headingId = React.useId()

  const def = navDefForPath(pathname)
  if (!def?.hasList) return null

  const resizable = tier === 'desktop' || tier === 'wide'

  return (
    <>
      <aside
        data-region="app.list"
        aria-labelledby={headingId}
        style={resizable ? { width: listWidth } : undefined}
        className={cn(
          'flex h-full min-h-0 flex-col bg-surface',
          resizable ? 'shrink-0' : 'w-full flex-1',
        )}
      >
        <h2 id={headingId} className="sr-only">
          {def.label}
        </h2>
        <SessionListColumn {...sessions} />
      </aside>
      {resizable && <SidebarResizer width={listWidth} onResize={setListWidth} />}
    </>
  )
}
