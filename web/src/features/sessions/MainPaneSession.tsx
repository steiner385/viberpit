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
 * - **Rename.** The phone list row has no rename button and the list is not on
 *   screen beside an open session, so the header is rename's only home there.
 *   Desktop gains a second route to a dialog it already had.
 *
 * The keyboard half is NOT here. PR 4 replaced `useInSessionKeys` — the surviving
 * copy of `DesktopShell`'s and `SessionView`'s detach handlers — with the shell's
 * one keydown listener, driven by `shell/shortcuts.ts`. This component publishes
 * `hasOpenAction` and a socket-detaching `onDetach` through
 * `shell/sessionGestures` and owns no listener of its own.
 *
 * The header row itself moved out to `SessionHeader` (Task 10), which carries
 * `data-region="session.header"` and owns the click-to-rename title and the
 * status pill. This component keeps what only it can know: the socket-detaching
 * `leave()` that every exit path runs through.
 */
import type { SessionStatus } from '@/api/types'
import { useReloadSession, useRotateSession } from '@/api/hooks'
import { useSessionSocket } from '@/ws/client'
import { SessionBody } from '@/components/session/SessionBody'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import { useSessionGestures } from '@/shell/sessionGestures'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'
import { SessionHeader } from './SessionHeader'

export interface MainPaneSessionProps {
  sessionId: string
  /** Archive/delete finished — the host advances to the neighbouring session. */
  onClosed: () => void
  /** Leave the session for the list (Back, ← / Esc / Ctrl+Z). */
  onDetach: () => void
  /** Open another session — the prev/next chevrons. */
  onOpenSession: (id: string) => void
}

export function MainPaneSession({ sessionId, onClosed, onDetach, onOpenSession }: MainPaneSessionProps) {
  const rt = useSessionRuntimeContext()
  const { detach } = useSessionSocket()
  const reloadSession = useReloadSession()
  const rotateSession = useRotateSession()

  // "Reload session": the daemon rebuilds the driver in place, then this client
  // re-attaches so the transcript is rebuilt from the daemon's state rather than
  // the stale one the user was staring at. The re-attach is the same `reattach`
  // the transcript's own "Retry" uses — one recovery path, two triggers.
  function reload() {
    reloadSession.mutate({ id: sessionId }, { onSuccess: () => rt.reattach() })
  }

  // "Next best account": same shape as reload — the daemon swaps the driver, then
  // this client re-attaches so the transcript reflects the daemon's state.
  function rotate() {
    rotateSession.mutate({ id: sessionId }, { onSuccess: () => rt.reattach() })
  }

  // Detaching the socket before navigating is what `SessionView` did. The
  // runtime provider also detaches when `PageHost` unmounts it, so this is
  // belt-and-braces — but it is the behaviour that ships today, and it is what
  // makes "archive detaches, then advances" true even when the host keeps the
  // provider mounted.
  function leave(after: () => void) {
    detach(sessionId)
    after()
  }

  // The keyboard half is the shell's single listener (`shell/useShortcuts`); this
  // publishes the two things it cannot see from up there — whether a card is
  // unanswered, and a detach that drops the socket first. Registering rather than
  // listening is what makes ← / Esc / Ctrl+Z one implementation with the rest of
  // the app's keys instead of a sixth window listener.
  useSessionGestures({
    hasOpenAction: rt.hasOpenAction,
    onDetach: () => leave(onDetach),
  })

  return (
    <div className="flex flex-col h-full min-h-0">
      <SessionHeader
        sessionId={sessionId}
        title={rt.title}
        goal={rt.goal}
        goalSource={rt.goalSource}
        // `state.status.state` is a plain string off the wire (an unknown value
        // is handled: statusPresentation falls back and warns once), so this is
        // the same narrowing `isPreSpawnStatus` makes in api/types.ts.
        status={rt.state.status.state as SessionStatus}
        onBack={() => leave(onDetach)}
        onOpenSession={onOpenSession}
        onClosed={() => leave(onClosed)}
        onReload={reload}
        onRotate={rotate}
      />
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-surface">
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
