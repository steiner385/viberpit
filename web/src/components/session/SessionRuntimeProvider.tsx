/**
 * SessionRuntimeProvider — runs useSessionRuntime once and shares the live runtime
 * with descendants. The desktop layout mounts this keyed by the focused session id
 * so each session gets a fresh reducer + a clean attach/detach cycle, while the main
 * pane (transcript) and the bottom prompt (composer) both read the same runtime.
 */

import { createContext, useContext } from 'react'
import { useSessionRuntime, type SessionRuntime } from '@/session/useSessionRuntime'
import type { SocketChannel } from '@/ws/client'

const Ctx = createContext<SessionRuntime | null>(null)

/**
 * `channel` picks the WebSocket the runtime attaches on. The AI drawer mounts its own
 * provider with `channel="assistant"` so it can run a second live session alongside the
 * page's one without the two stealing each other's attachment.
 */
export function SessionRuntimeProvider({
  sessionId, initialPrompt, channel = 'route', children,
}: { sessionId: string; initialPrompt?: string; channel?: SocketChannel; children: React.ReactNode }) {
  const rt = useSessionRuntime(sessionId, initialPrompt, channel)
  return <Ctx.Provider value={rt}>{children}</Ctx.Provider>
}

// Colocating the context hook with its provider is the conventional pattern.
// eslint-disable-next-line react-refresh/only-export-components
export function useSessionRuntimeContext(): SessionRuntime {
  const v = useContext(Ctx)
  if (!v) throw new Error('useSessionRuntimeContext must be used within SessionRuntimeProvider')
  return v
}
