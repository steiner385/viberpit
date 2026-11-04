/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider and the hooks that read/write into it are two halves of one contract.
   Same convention as `shell/sessionGestures.tsx`, which this mirrors. */
/**
 * The Assistant drawer's two bridges to the rest of the shell.
 *
 * 1. **Escape** (below, first): how the open AssistantSheet hands its live-turn state
 *    to `DrawerHost`, so Escape can stop the assistant's turn before closing the drawer
 *    instead of abandoning it mid-response.
 * 2. **Open-and-send** (D-12, `requestAssistant`): THE path for "open the Assistant
 *    drawer and optionally send it a message" — `/btw` in a session's composer is its
 *    first caller. See `AssistantRequestProvider`.
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
import { useOpenDrawer } from './drawerParam'

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

// --- Open-and-send (D-12) --------------------------------------------------------------

/**
 * A message waiting for the Assistant sheet to send it. A fresh object per request, so
 * the same text asked twice is two sends, not one deduplicated by value.
 */
export interface PendingAssistantSend {
  readonly text: string
}

/**
 * Why a store in context rather than a ref (the Escape bridge's shape): the writer
 * (`/btw` in the dock composer) and the reader (`AssistantSheet`) are both mounted
 * below the shell, and the reader has to RE-RENDER when a send is parked — the sheet
 * may already be open and idle. Why not the URL, like the drawer itself: a send in a
 * `?param=` would replay on reload, Back, or any shared link, and could be planted by
 * one. Nothing here reads the URL; `requestAssistant` is the only writer.
 *
 * Exactly-once is `take()`: it clears the slot as it returns the text, so the sheet
 * remounting, re-rendering or running its effect twice (StrictMode) finds nothing left.
 */
interface AssistantRequestStore {
  get: () => PendingAssistantSend | null
  set: (send: PendingAssistantSend | null) => void
  subscribe: (cb: () => void) => () => void
}

function createAssistantRequestStore(): AssistantRequestStore {
  let pending: PendingAssistantSend | null = null
  const listeners = new Set<() => void>()
  return {
    get: () => pending,
    set: (send) => { pending = send; listeners.forEach((l) => l()) },
    subscribe: (cb) => { listeners.add(cb); return () => { listeners.delete(cb) } },
  }
}

const AssistantRequestContext = React.createContext<AssistantRequestStore | null>(null)

/** Owns the pending-send slot. Mounted by `AppShell` around the whole shell, because
 *  the writer (the dock composer) and the reader (the drawer) are siblings there. */
export function AssistantRequestProvider({ children }: { children: React.ReactNode }) {
  const [store] = React.useState(createAssistantRequestStore)
  return <AssistantRequestContext.Provider value={store}>{children}</AssistantRequestContext.Provider>
}

/**
 * Open the Assistant drawer (`openDrawer('assistant')` — the same open ⌘; does) and,
 * with a non-blank `send`, park that text for the sheet to send as its next prompt.
 * Outside the provider it still opens the drawer; there is no sheet to send from.
 */
export function useRequestAssistant(): (request?: { send?: string }) => void {
  const store = React.useContext(AssistantRequestContext)
  const openDrawer = useOpenDrawer()
  return React.useCallback((request?: { send?: string }) => {
    const text = request?.send?.trim() ?? ''
    if (store && text) store.set({ text })
    openDrawer('assistant')
  }, [store, openDrawer])
}

const NO_SEND = { get: () => null, set: () => {}, subscribe: () => () => {} } satisfies AssistantRequestStore

/**
 * The sheet's side: the parked send (re-renders when one arrives) and `take`, which
 * returns its text and clears it — `null` when there is nothing, or it was taken.
 * Inert outside the provider.
 */
export function usePendingAssistantSend(): {
  pendingSend: PendingAssistantSend | null
  take: () => string | null
} {
  const store = React.useContext(AssistantRequestContext) ?? NO_SEND
  const pendingSend = React.useSyncExternalStore(store.subscribe, store.get, store.get)
  const take = React.useCallback(() => {
    const pending = store.get()
    if (pending === null) return null
    store.set(null)
    return pending.text
  }, [store])
  return { pendingSend, take }
}
