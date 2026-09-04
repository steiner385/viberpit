/**
 * SessionHeader — the open session's top bar: Back, the prev/next chevrons, the
 * title (click to rename), the live status pill and `SessionActions`.
 *
 * Extracted out of `MainPaneSession` (Task 10). The markup is the one that
 * shipped — Back, the chevrons and `SessionActions` are unchanged, down to the
 * accessible names their tests query by — plus the two things this task adds:
 * a click-to-rename title button and a `StatusPill`.
 *
 * ## `data-region="session.header"` (DEC-1)
 *
 * The first `session.*` region. The shell's `REGION_IDS` stays the closed
 * 7-tuple of `app.*` regions it has always been: those are the regions the
 * SHELL places, and the visibility matrix is the contract for them. `session.*`
 * regions are page-owned — the page decides whether they exist, the shell never
 * places or hides one — so widening `RegionId` to hold this would have made the
 * matrix answer for a region it does not control. The shell's three region-set
 * equality assertions are scoped to the `app.` prefix for the same reason.
 *
 * ## Rename has ONE implementation
 *
 * There are two triggers — the title button here and the `⋯` menu item in
 * `SessionActions` — and one flow: the `renameOpen` state and the shared
 * `features/sessions/RenameDialog` below, driven by `useRenameSession`. Task 10
 * moved that flow OUT of `SessionActions` (which now takes an `onRename`
 * callback instead of a `canRename` flag) rather than adding a second copy of
 * it beside the title.
 *
 * ## Why the pill is here as well as in the meta strip
 *
 * `SessionBody`'s meta strip carries a `StatusBar` — the live, `aria-live`
 * status LINE (working spinner, sub-agent phrase, connection note). The pill is
 * the at-a-glance tier badge for the header, and both read the same
 * `session/statusTier` table, so they cannot disagree: one source, two
 * presentations. The pill is `aria-hidden` free but silent for screen readers
 * being announced twice is not a risk — `StatusBar` is the only `role="status"`
 * live region; the pill is a static label.
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import type { SessionStatus } from '@/api/types'
import { useRenameSession } from '@/api/hooks'
import { StatusPill } from '@/components/UsageBadge'
import { SessionGoalText } from '@/components/session/SessionGoalText'
import { SessionActions } from '@/components/session/SessionActions'
import { useSessionNeighbors } from '@/session/useSessionNeighbors'
import { RenameDialog } from './RenameDialog'

export interface SessionHeaderProps {
  sessionId: string
  /** The session's display title (`rt.title`). Also the rename dialog's seed. */
  title: string
  /** The reckoned goal, rendered beside the title when it differs from it. */
  goal: string | null
  goalSource: string | null
  /** Live session status (`rt.state.status.state`) — drives the pill's tier. */
  status: SessionStatus
  /** Leave the session for the list (the Back button). */
  onBack: () => void
  /** Open another session — the prev/next chevrons. */
  onOpenSession: (id: string) => void
  /** Archive/delete finished — the host advances to the neighbouring session. */
  onClosed: () => void
  /** "Reload session" menu item — the host rebuilds the driver and re-attaches. */
  onReload?: () => void
  /** "Next best account" menu item — the host rotates the account and re-attaches. */
  onRotate?: () => void
}

const NAV_BUTTON = cn(
  'flex items-center justify-center rounded-md min-h-target min-w-target',
  'text-sm font-medium text-ink-muted hover:text-ink hover:bg-surface',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)

export function SessionHeader({
  sessionId, title, goal, goalSource, status, onBack, onOpenSession, onClosed, onReload, onRotate,
}: SessionHeaderProps) {
  const { prevId, nextId } = useSessionNeighbors(sessionId)
  const [renameOpen, setRenameOpen] = useState(false)
  const renameSession = useRenameSession()

  // Both chevrons render as a pair: useSessionNeighbors returns prevId/nextId
  // both-null (single/unknown session) or both-set (wraparound), never one null.
  const hasNeighbors = prevId !== null || nextId !== null

  return (
    <div
      data-region="session.header"
      className={cn(
        'flex items-center gap-2 px-3 py-2 flex-shrink-0',
        'bg-surface-raised border-b border-edge',
      )}
    >
      <button
        type="button"
        onClick={onBack}
        aria-label="Back to session list"
        className={cn(NAV_BUTTON, 'gap-1.5 px-3')}
      >
        ← Back
      </button>

      {hasNeighbors && (
        <button
          type="button"
          onClick={() => prevId && onOpenSession(prevId)}
          aria-label="Previous session"
          className={cn(NAV_BUTTON, 'text-lg leading-none')}
        >
          ‹
        </button>
      )}

      <span className="flex-1 min-w-0 flex items-baseline gap-2">
        {/* A real <button>, not a div with onClick: Enter/Space, focus order and
            the button role all come free, and the accessible name names the
            session so "Rename session" is never ambiguous in a screen-reader
            control list. */}
        <button
          type="button"
          onClick={() => setRenameOpen(true)}
          aria-label={`Rename session ${title}`}
          className={cn(
            'min-w-0 truncate rounded-md px-1 text-left text-sm font-semibold text-ink',
            'hover:bg-surface',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
        >
          {title}
        </button>
        <SessionGoalText goal={goal} source={goalSource} title={title} />
      </span>

      <StatusPill status={status} />

      {hasNeighbors && (
        <button
          type="button"
          onClick={() => nextId && onOpenSession(nextId)}
          aria-label="Next session"
          className={cn(NAV_BUTTON, 'text-lg leading-none')}
        >
          ›
        </button>
      )}

      <SessionActions
        sessionId={sessionId}
        sessionName={title}
        onDone={onClosed}
        variant="light"
        onRename={() => setRenameOpen(true)}
        onReload={onReload}
        onRotate={onRotate}
      />

      {/* The one rename flow. Both triggers above open THIS dialog; unlike
          archive/delete it stays in place (no onClosed), so the header just
          refreshes its title. */}
      {renameOpen && (
        <RenameDialog
          sessionId={sessionId}
          currentName={title}
          onClose={() => setRenameOpen(false)}
          onSubmit={(id, name) =>
            renameSession.mutate({ id, name }, { onSuccess: () => setRenameOpen(false) })
          }
          isPending={renameSession.isPending}
        />
      )}
    </div>
  )
}
