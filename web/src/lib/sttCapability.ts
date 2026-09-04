import { useSyncExternalStore } from 'react'

/**
 * Whether the edge offers the server-side dictation engine (`/api/config.sttEnabled`).
 *
 * A tiny external store rather than `useConfig()` inside `useDictationField`: that hook
 * is rendered by Composer/DispatchBar in many tests (and code paths) with no
 * QueryClientProvider. AppShell — which already reads config — writes it once per change.
 */
let enabled = false
const listeners = new Set<() => void>()

export function getServerSttEnabled(): boolean { return enabled }

export function setServerSttEnabled(v: boolean): void {
  if (enabled === v) return
  enabled = v
  listeners.forEach((l) => l())
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => { listeners.delete(l) }
}

export function useServerSttEnabled(): boolean {
  return useSyncExternalStore(subscribe, getServerSttEnabled, getServerSttEnabled)
}
