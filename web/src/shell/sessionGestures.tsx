/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider and the hook that registers into it are two halves of one contract,
   and splitting them would put the shell's side and the session's side of the
   same bridge in two files. Same convention as `shell/InteractionContext.tsx`. */
/**
 * How the open session hands its two facts to the shell's one keydown listener.
 *
 * ## Why a bridge is needed at all
 *
 * The in-session gestures (Esc / ← / Ctrl+Z → detach) need `hasOpenAction` — is
 * a permission or question card unanswered — and a detach that first detaches the
 * socket. Both belong to the session runtime, and the runtime provider is mounted
 * by `PageHost`, BELOW the shell (deliberately: audit F2.3 scoped it there so
 * selecting a session stops remounting the whole shell). So the shell cannot read
 * it from context, and the router gives no way to pass a prop upwards.
 *
 * A module-level store (the shape `useOverlayState` uses) would work, but this is
 * per-open-session state rather than a global count, and a leaked value here
 * means Escape silently stops detaching — or detaches over an unanswered card.
 * Context keeps its lifetime tied to the tree that owns it.
 *
 * ## Why a ref, not state
 *
 * The listener runs outside render and reads through `ctx()` at keydown time, so
 * nothing has to re-render when these change. Publishing them as state would
 * re-render the whole shell on every `hasOpenAction` flip — and on every
 * `onDetach` identity change, which is every render of `MainPaneSession`.
 */
import * as React from 'react'

export interface SessionGestures {
  /** A permission or question card is waiting for an answer. */
  hasOpenAction: boolean
  /** Leave the session for the list (the click equivalent is Back). */
  onDetach: () => void
}

/**
 * Null outside the shell. A registering component then simply does not register,
 * which is what makes `MainPaneSession` mountable in a unit test without the
 * shell — and, because the shell's `sessionOpen` is "a handle is registered",
 * that also means no detach row can fire with nothing to detach.
 */
const SessionGestureContext = React.createContext<React.RefObject<SessionGestures | null> | null>(
  null,
)

/** The shell's side: owns the ref both halves share. */
export function SessionGestureProvider({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<SessionGestures | null>(null)
  return <SessionGestureContext.Provider value={ref}>{children}</SessionGestureContext.Provider>
}

/**
 * The shell's read handle — `null` current means no session is open, which is
 * exactly what `ShortcutContext.sessionOpen` reports.
 *
 * Throws outside the provider rather than returning an empty ref: a shell that
 * silently read a handle nobody writes would have Esc/←/Ctrl+Z permanently
 * inert, with no symptom to find.
 */
export function useSessionGestureHandle(): React.RefObject<SessionGestures | null> {
  const ref = React.useContext(SessionGestureContext)
  if (!ref) throw new Error('useSessionGestureHandle must be used inside SessionGestureProvider')
  return ref
}

/**
 * The open session's side: publish this session's gestures for as long as it is
 * mounted.
 *
 * Two effects on purpose. The first has no dep array, so the published values
 * are never a render behind — `hasOpenAction` flips the instant a card arrives,
 * and the very next Escape must see it. The second unregisters on unmount ONLY:
 * folding it into the first would clear the handle after every render of the
 * pane, and a keypress landing in that window would find no session open.
 */
export function useSessionGestures(gestures: SessionGestures): void {
  const ref = React.useContext(SessionGestureContext)

  React.useEffect(() => {
    if (ref) ref.current = gestures
  })

  React.useEffect(() => {
    if (!ref) return
    return () => {
      ref.current = null
    }
  }, [ref])
}
