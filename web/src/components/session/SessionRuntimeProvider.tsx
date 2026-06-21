/**
 * SessionRuntimeProvider — runs useSessionRuntime once and shares the live runtime
 * with descendants. The desktop layout mounts this keyed by the focused session id
 * so each session gets a fresh reducer + a clean attach/detach cycle, while the main
 * pane (transcript) and the bottom prompt (composer) both read the same runtime.
 */

import { createContext, useContext } from 'react'
import { useSessionRuntime, type SessionRuntime } from '@/session/useSessionRuntime'

const Ctx = createContext<SessionRuntime | null>(null)

export function SessionRuntimeProvider({
  sessionId, children,
}: { sessionId: string; children: React.ReactNode }) {
  const rt = useSessionRuntime(sessionId)
  return <Ctx.Provider value={rt}>{children}</Ctx.Provider>
}

// Colocating the context hook with its provider is the conventional pattern.
// eslint-disable-next-line react-refresh/only-export-components
export function useSessionRuntimeContext(): SessionRuntime {
  const v = useContext(Ctx)
  if (!v) throw new Error('useSessionRuntimeContext must be used within SessionRuntimeProvider')
  return v
}
