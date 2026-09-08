// web/src/features/sessions/ScopeBody.tsx
//
// The two-part frame each scope body of `SessionListColumn` fills: a fixed
// controls strip above one scrolling band. `LiveScopeBody` (in
// `SessionListColumn.tsx`) and `ArchivedScopeBody` had written this frame out
// twice, byte-for-byte the same two class strings and the same `data-testid` —
// the one place the scope-switch consolidation did not land. Shared here the
// way `EmptyPane` shares the empty-page layout between `WelcomePane` and
// `ArchivedWelcome`: one place a future change to the frame has to land.
//
// `controls` gets its own `empty:hidden` strip so a scope with no sessions
// paints no bare toolbar above the scroll band — each caller still decides
// WHETHER to pass controls (gated on its own session count), this only owns
// the wrapper around whatever it is handed.
import type { ReactNode } from 'react'

export interface ScopeBodyProps {
  /** The controls strip's contents, or nothing — `empty:hidden` collapses the
   *  strip entirely when the caller passes nothing (e.g. an empty list). */
  controls?: ReactNode
  /** The scrolling band's contents — sections, rows, the empty/no-match state. */
  children: ReactNode
}

export function ScopeBody({ controls, children }: ScopeBodyProps) {
  return (
    <>
      {/* The list controls stay above the scroll band. `empty:hidden` because
          the controls are conditional and this strip's only child — without it
          a session-less list paints a bare strip above the scroll area, which
          reads as a control that failed to render.

          `short:hidden` because this strip is the one piece of the column that
          is optional. Everything else here is `shrink-0` too — the scope
          segment, the daemon line, New Session — but each is either the way out
          of the scope or the thing the column is for. The strip is three
          wrapped rows (~125px of a phone's ~200px column) of search, three
          filters, group-by and sort-by, and it is the least useful of them at
          the moment it costs the most: the viewport is only this short because
          the keyboard is up, which means someone is typing a prompt, not
          narrowing a list. Without this the scroll band collapsed to ~8px and
          every session was unreachable behind the dock.

          It is hidden, not unmounted, so the filters keep whatever state they
          were carrying and the rows come straight back when the keyboard does.
          Nothing here is the ONLY way to reach anything: search, filters and
          ordering all persist, and the list itself is the thing being restored.

          Both scopes get it because both wrap their controls in this strip —
          the reason the strip lives here rather than in each scope body. */}
      <div
        // Addressable so the two contracts this strip carries (the variant and
        // the `empty:hidden` collapse) can be asserted in jsdom, which does no
        // layout — same reason the scroll band below has one.
        data-testid="session-list-controls"
        className="shrink-0 space-y-2 px-2 pb-2 empty:hidden short:hidden"
      >
        {controls}
      </div>

      {/* The only scrolling band. Both scopes share one `data-testid` — never
          both mounted at once, since `SessionListColumn` renders the two
          bodies as siblings gated on `scope` — and the scroll-preservation
          assertions in ContextList.test.tsx key off it. Keep it. */}
      <div
        data-testid="session-list-scroll"
        className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2"
      >
        {children}
      </div>
    </>
  )
}
