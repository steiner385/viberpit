/**
 * MainPaneSession — the open session's page body: nav row, title, actions, transcript.
 *
 * Moved verbatim out of `components/DesktopShell.tsx` (Task 17) when that shell
 * was deleted; `shell/routes.tsx` has rendered it for `/sessions/:id` since Task
 * 15. It READS the session runtime from context rather than creating one —
 * `PageHost` owns the single `SessionRuntimeProvider` (audit F2.3), and the
 * `PromptDock` composer reads the same instance, so the transcript and the
 * prompt bar can never end up attached to two different sockets.
 *
 * It does NOT render a composer: on this shell the prompt bar is `app.dock`,
 * placed by the visibility matrix, not by the page.
 *
 * ## What Task 18 folded in from `SessionView`
 *
 * `SessionView` was the phone half of the old fork and had no call site left
 * after Task 17, so it is deleted. Three things it owned are NOT redundant with
 * this shell and would have been silently lost with it, so they live here now —
 * in one component, in every layout mode, rather than in a phone-only twin:
 *
 * - **Back.** The click equivalent of the ← / Esc detach gesture. On phone it is
 *   the only way out of an open session that is not the bottom nav (the list
 *   region is dropped once an item is open); on desktop it is what
 *   `DesktopShell`'s ← already did — return to the welcome pane beside the list.
 * - **The prev/next chevrons.** `useSessionNeighbors` ordering, with wraparound.
 * - **`canRename`.** The phone list row has no rename button and the list is not
 *   on screen beside an open session, so the overflow menu is rename's only home
 *   there. Desktop gains a second route to a dialog it already had.
 *
 * The keyboard half is `useInSessionKeys`, which is also the surviving copy of
 * `DesktopShell`'s own detach handler.
 */
import { cn } from '@/lib/utils'
import { useSessionSocket } from '@/ws/client'
import { useSessionNeighbors } from '@/session/useSessionNeighbors'
import { SessionGoalText } from '@/components/session/SessionGoalText'
import { SessionActions } from '@/components/session/SessionActions'
import { SessionBody } from '@/components/session/SessionBody'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import { useInSessionKeys } from '@/components/session/useInSessionKeys'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'

export interface MainPaneSessionProps {
  sessionId: string
  /** Archive/delete finished — the host advances to the neighbouring session. */
  onClosed: () => void
  /** Leave the session for the list (Back, ← / Esc / Ctrl+Z). */
  onDetach: () => void
  /** Open another session — the prev/next chevrons. */
  onOpenSession: (id: string) => void
}

const NAV_BUTTON = cn(
  'flex items-center justify-center rounded-md min-h-target min-w-target',
  'text-sm font-medium text-ink-muted hover:text-ink hover:bg-surface-raised',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)

export function MainPaneSession({ sessionId, onClosed, onDetach, onOpenSession }: MainPaneSessionProps) {
  const rt = useSessionRuntimeContext()
  const { detach } = useSessionSocket()
  const { prevId, nextId } = useSessionNeighbors(sessionId)
  // Both chevrons render as a pair: useSessionNeighbors returns prevId/nextId
  // both-null (single/unknown session) or both-set (wraparound), never one null.
  const hasNeighbors = prevId !== null || nextId !== null

  // Detaching the socket before navigating is what `SessionView` did. The
  // runtime provider also detaches when `PageHost` unmounts it, so this is
  // belt-and-braces — but it is the behaviour that ships today, and it is what
  // makes "archive detaches, then advances" true even when the host keeps the
  // provider mounted.
  function leave(after: () => void) {
    detach(sessionId)
    after()
  }

  useInSessionKeys({
    hasOpenAction: rt.hasOpenAction,
    onDetach: () => leave(onDetach),
  })

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-2 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <button
          type="button"
          onClick={() => leave(onDetach)}
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
          <span className="min-w-0 text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
          <SessionGoalText goal={rt.goal} source={rt.goalSource} title={rt.title} />
        </span>

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
          sessionName={rt.title}
          onDone={() => leave(onClosed)}
          variant="light"
          canRename
        />
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-white dark:bg-gray-950">
        <SessionBody rt={rt} />
      </div>
      <CommandResultOverlay
        result={rt.commandResult}
        commands={rt.commands}
        onClose={rt.dismissCommandResult}
      />
    </div>
  )
}
