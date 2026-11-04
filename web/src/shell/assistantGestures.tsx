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
import { subscribeDrawerClosed, useOpenDrawer } from './drawerParam'

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
 * A message waiting for the Assistant sheet to send it, tied to the session it was
 * asked about: the sheet sends it only while that session is still the open one, with
 * that session as its context. A fresh object per request, so the same text asked
 * twice is two sends, not one deduplicated by value.
 */
export interface PendingAssistantSend {
  readonly text: string
  /** The session `/btw` was typed in — the side chat's scope for this message. */
  readonly sessionId: string | null
}

/**
 * The drawer's side-chat state that outlives one mount of the sheet: the parked send,
 * and the session the drawer's last sent context named (`undefined`: nothing sent yet).
 *
 * Why a store in context rather than a ref (the Escape bridge's shape): the writer
 * (`/btw` in the dock composer) and the reader (`AssistantSheet`) are both mounted
 * below the shell, and the reader has to RE-RENDER when a send is parked — the sheet
 * may already be open and idle. Why not the URL, like the drawer itself: a send in a
 * `?param=` would replay on reload, Back, or any shared link, and could be planted by
 * one. Nothing here reads the URL; `requestAssistant` is the only writer.
 *
 * Exactly-once is `take()`: it clears the slot as it returns the send, so the sheet
 * remounting, re-rendering or running its effect twice (StrictMode) finds nothing left.
 * Closing the Assistant (`useCloseDrawer`) clears it too: a question parked behind a
 * drawer the user dismissed is never sent later. Help opened over it is not a close.
 */
interface AssistantRequestState {
  pending: PendingAssistantSend | null
  sentScope: string | null | undefined
}

interface AssistantRequestStore {
  get: () => AssistantRequestState
  update: (patch: Partial<AssistantRequestState>) => void
  subscribe: (cb: () => void) => () => void
}

function createAssistantRequestStore(): AssistantRequestStore {
  let state: AssistantRequestState = { pending: null, sentScope: undefined }
  const listeners = new Set<() => void>()
  return {
    get: () => state,
    update: (patch) => { state = { ...state, ...patch }; listeners.forEach((l) => l()) },
    subscribe: (cb) => { listeners.add(cb); return () => { listeners.delete(cb) } },
  }
}

const AssistantRequestContext = React.createContext<AssistantRequestStore | null>(null)

/** Owns the side-chat state. Mounted by `AppShell` around the whole shell, because
 *  the writer (the dock composer) and the reader (the drawer) are siblings there. */
export function AssistantRequestProvider({ children }: { children: React.ReactNode }) {
  const [store] = React.useState(createAssistantRequestStore)
  React.useEffect(() => subscribeDrawerClosed((name) => {
    if (name === 'assistant' && store.get().pending) store.update({ pending: null })
  }), [store])
  return <AssistantRequestContext.Provider value={store}>{children}</AssistantRequestContext.Provider>
}

/**
 * Open the Assistant drawer (`openDrawer('assistant')` — the same open ⌘; does) and,
 * with a non-blank `send`, park that text, tied to `sessionId`, for the sheet to send
 * as its next prompt. Outside the provider it still opens the drawer; there is no
 * sheet to send from.
 */
export function useRequestAssistant(): (request?: { send?: string; sessionId?: string | null }) => void {
  const store = React.useContext(AssistantRequestContext)
  const openDrawer = useOpenDrawer()
  return React.useCallback((request?: { send?: string; sessionId?: string | null }) => {
    const text = request?.send?.trim() ?? ''
    if (store && text) store.update({ pending: { text, sessionId: request?.sessionId ?? null } })
    openDrawer('assistant')
  }, [store, openDrawer])
}

const NO_STATE: AssistantRequestState = { pending: null, sentScope: undefined }
const INERT_STORE = {
  get: () => NO_STATE, update: () => {}, subscribe: () => () => {},
} satisfies AssistantRequestStore

function useRequestState(): [AssistantRequestState, AssistantRequestStore] {
  const store = React.useContext(AssistantRequestContext) ?? INERT_STORE
  return [React.useSyncExternalStore(store.subscribe, store.get, store.get), store]
}

/**
 * The sheet's side: the parked send (re-renders when one arrives) and `take`, which
 * returns it and clears it — `null` when there is nothing, or it was taken.
 * Inert outside the provider.
 */
export function usePendingAssistantSend(): {
  pendingSend: PendingAssistantSend | null
  take: () => PendingAssistantSend | null
} {
  const [{ pending }, store] = useRequestState()
  const take = React.useCallback(() => {
    const parked = store.get().pending
    if (parked !== null) store.update({ pending: null })
    return parked
  }, [store])
  return { pendingSend: pending, take }
}

/**
 * The session the drawer's last sent context named — the scope the server holds for
 * the turn it is running (D-12) — and the recorder every drawer send goes through.
 * `undefined` until the drawer has sent anything; `null` for a context naming none.
 */
export function useAssistantSentScope(): {
  sentScope: string | null | undefined
  recordSentScope: (sessionId: string | null) => void
} {
  const [{ sentScope }, store] = useRequestState()
  const recordSentScope = React.useCallback((sessionId: string | null) => {
    if (store.get().sentScope !== sessionId) store.update({ sentScope: sessionId })
  }, [store])
  return { sentScope, recordSentScope }
}
