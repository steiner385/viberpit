import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'
import { useUpdatePending } from '@/hooks/useUpdatePending'

export interface InputHoldState {
  holding: boolean
  count: number
  mode: 'updating' | 'reconnecting'
}

function tryGetSocket() {
  try {
    return getSessionSocket()
  } catch (e) {
    // jsdom rejects relative WebSocket URLs ('/ws') with SYNTAX_ERR; degrade under test.
    if (e instanceof DOMException && e.code === DOMException.SYNTAX_ERR) return null
    throw e
  }
}

/**
 * True whenever the user's input cannot currently reach a session, so the UI should hold a
 * banner: a deploy drain is in progress, OR prompts are already buffered, OR the socket dropped
 * *after* being connected and is reconnecting.
 *
 * We deliberately key the disconnect case on the `reconnecting` state, NOT `!connected`: a fresh
 * socket starts in `connecting`/`disconnected` during the initial handshake, and treating that as
 * "input held" would flash the banner on every page load (and render a spurious second
 * role="status" in Dashboard tests). `reconnecting` only occurs after a prior connection dropped.
 */
export function useInputHold(): InputHoldState {
  const pending = useUpdatePending()
  const socket = tryGetSocket()
  const [conn, setConn] = useState<ConnectionState>(socket ? socket.connectionState : 'connected')
  useEffect(() => {
    if (!socket) return
    const ch = (s: ConnectionState) => setConn(s)
    socket.on('connectionStateChange', ch)
    return () => socket.off('connectionStateChange', ch)
  }, [socket])
  const reconnecting = conn === 'reconnecting'
  return {
    holding: pending.active || pending.count > 0 || reconnecting,
    count: pending.count,
    // 'updating' is the richer deploy message; otherwise we're just reconnecting.
    mode: pending.active ? 'updating' : 'reconnecting',
  }
}
