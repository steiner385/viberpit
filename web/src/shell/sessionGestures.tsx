/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider and the hook that registers into it are two halves of one contract,
   and splitting them would put the shell's side and the session's side of the
   same bridge in two files. Same convention as `shell/InteractionContext.tsx`. */
/**
 * How the open session hands its gestures to the shell's one keydown listener.
 *
 * ## Why a bridge is needed at all
 *
 * The in-session gestures (← / Ctrl+Z → detach; Esc → stop a running turn, else
 * detach; Ctrl/⌘+O → cycle the transcript view) need `hasOpenAction` (is a
 * permission or question card unanswered), `busy` (is a turn running), a stop, a
 * detach that first detaches the socket, and the view-mode cycle. All of them
 * belong to the session runtime, and the runtime
 * provider is mounted by `PageHost`, BELOW the shell (deliberately: audit F2.3 scoped it there so
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
import type { PromptRowMenus } from '@/features/sessions/composer/PromptRow'
import type { ComposerAttach } from '@/features/sessions/composer/Composer'

export interface SessionGestures {
  /** A permission or question card is waiting for an answer. */
  hasOpenAction: boolean
  /** A turn is running — the composer's Stop button is showing. Esc stops it. */
  busy: boolean
  /** Stop the running turn (the click equivalent is the composer's Stop). */
  onStop: () => void
  /** A non-plan permission card is waiting — what ⌘. / Ctrl+. would deny. */
  hasDeniablePermission: boolean
  /** Deny the oldest pending non-plan permission card (the click equivalent is its Deny). */
  denyOldestPermission: () => void
  /** Leave the session for the list (the click equivalent is Back). */
  onDetach: () => void
  /** Advance the transcript view mode (Ctrl/⌘+O; the click equivalent is the View menu). */
  onCycleView: () => void
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

/**
 * The prompt row's menu openers (Alt+Shift+M / I / E). They live beside the
 * gestures rather than in them because the OTHER half is the dock's composer,
 * not `MainPaneSession`: the row is mounted by `PromptDock`, a sibling of the
 * pane, so neither could hand it to the other. Same lifetime rule, same bridge.
 * Null current means no prompt row is mounted, and the chords then do nothing.
 */
const PromptRowMenusContext = React.createContext<React.RefObject<PromptRowMenus | null> | null>(
  null,
)

/**
 * The dock composer's attach handle (D-7): a transcript path's "Attach as @"
 * writes into the composer's draft. Same shape of problem as the menus above —
 * the transcript (`SessionBody`, in `app.main`) and the composer (`PromptDock`)
 * are siblings — so the same bridge. Null current means no composer is mounted
 * and an attach does nothing.
 */
const ComposerAttachContext = React.createContext<React.RefObject<ComposerAttach | null> | null>(
  null,
)

/** The shell's side: owns the refs both halves share. */
export function SessionGestureProvider({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<SessionGestures | null>(null)
  const menus = React.useRef<PromptRowMenus | null>(null)
  const attach = React.useRef<ComposerAttach | null>(null)
  return (
    <SessionGestureContext.Provider value={ref}>
      <PromptRowMenusContext.Provider value={menus}>
        <ComposerAttachContext.Provider value={attach}>{children}</ComposerAttachContext.Provider>
      </PromptRowMenusContext.Provider>
    </SessionGestureContext.Provider>
  )
}

/** The dock composer's side: the ref to pass as `SessionComposer`'s `attachRef`.
 *  Undefined outside the shell. */
export function useComposerAttachRegistration(): React.RefObject<ComposerAttach | null> | undefined {
  return React.useContext(ComposerAttachContext) ?? undefined
}

/**
 * The transcript's side: attach `path` to the open session's composer draft as
 * `@path`. Undefined outside the shell (no composer can exist), so a caller can
 * leave the action out rather than offer one that does nothing.
 */
export function useComposerAttach(): ((path: string) => void) | undefined {
  const ref = React.useContext(ComposerAttachContext)
  const attach = React.useCallback((path: string) => ref?.current?.attachFile(path), [ref])
  return ref ? attach : undefined
}

/** The shell's read handle for the prompt row's menus (see `PromptRowMenusContext`). */
export function usePromptRowMenusHandle(): React.RefObject<PromptRowMenus | null> {
  const ref = React.useContext(PromptRowMenusContext)
  if (!ref) throw new Error('usePromptRowMenusHandle must be used inside SessionGestureProvider')
  return ref
}

/**
 * The dock composer's side: the ref to pass as `SessionComposer`'s
 * `promptRowMenus`. Undefined outside the shell, so the composer stays
 * mountable in a unit test without it.
 */
export function usePromptRowMenusRegistration(): React.RefObject<PromptRowMenus | null> | undefined {
  return React.useContext(PromptRowMenusContext) ?? undefined
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
