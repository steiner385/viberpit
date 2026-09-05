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
          reads as a control that failed to render. */}
      <div className="shrink-0 space-y-2 px-2 pb-2 empty:hidden">{controls}</div>

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
