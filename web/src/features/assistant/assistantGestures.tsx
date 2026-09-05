/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider and the hooks that read/write into it are two halves of one contract.
   Same convention as `shell/sessionGestures.tsx`, which this mirrors. */
/**
 * How the open AssistantSheet hands its live-turn state to `DrawerHost`, so Escape
 * can stop the assistant's turn before closing the drawer instead of abandoning it
 * mid-response.
 *
 * Same bridge shape as `shell/sessionGestures.tsx` (a ref in context, written by the
 * component that has the state, read by the shell's close handler), with one
 * deliberate difference: unlike an open session, there may legitimately be no
 * assistant sheet mounted (the drawer might be closed, or open on Help instead) — so
 * neither hook throws outside a writer. `useAssistantGestureHandle()` returns an
 * inert ref (`current: null`) rather than requiring `DrawerHost` to know whether the
 * assistant drawer specifically is the one open.
 */
import * as React from 'react'

export interface AssistantGestures {
  /** The assistant's own turn is in flight (mirrors `statusTier(...) === 'busy'`). */
  turnLive: boolean
  /** Stop the in-flight turn. */
  interrupt: () => void
}

const AssistantGestureContext = React.createContext<React.RefObject<AssistantGestures | null> | null>(
  null,
)

/** Owns the ref; wrap it around `DrawerHost` so its close handler and the nested
 *  `AssistantSheet` (whenever the assistant drawer happens to be open) share one ref. */
export function AssistantGestureProvider({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<AssistantGestures | null>(null)
  return <AssistantGestureContext.Provider value={ref}>{children}</AssistantGestureContext.Provider>
}

/** A ref whose `.current` is always `null` — handed back outside a provider, or when
 *  no `AssistantSheet` has published gestures yet, so callers never have to branch on
 *  "is there a provider" separately from "is there a live turn". */
const INERT_REF: React.RefObject<AssistantGestures | null> = { current: null }

/** The shell's read handle. Never throws: a closed/absent assistant sheet is a valid,
 *  common state, not a wiring bug. */
export function useAssistantGestureHandle(): React.RefObject<AssistantGestures | null> {
  const ref = React.useContext(AssistantGestureContext)
  return ref ?? INERT_REF
}

/**
 * The assistant sheet's side: publish its gestures for as long as it is mounted.
 *
 * Two effects on purpose (mirrors `useSessionGestures`). The first has no dep array
 * so the published values are never a render behind — `turnLive` flips the instant a
 * turn starts/ends, and the very next Escape must see it. The second clears the
 * handle on unmount ONLY, so a keypress mid-render doesn't find a stale write.
 */
export function useAssistantGestures(gestures: AssistantGestures): void {
  const ref = React.useContext(AssistantGestureContext)

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
