import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'

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

export function useInputHold(): InputHoldState {
  const socket = tryGetSocket()
  const [pending, setPending] = useState(socket ? socket.updatePendingState : { active: false, count: 0 })
  const [conn, setConn] = useState<ConnectionState>(socket ? socket.connectionState : 'connected')
  useEffect(() => {
    if (!socket) return
    const ph = (s: { active: boolean; count: number }) => setPending(s)
    const ch = (s: ConnectionState) => setConn(s)
    socket.on('updatePendingChange', ph)
    socket.on('connectionStateChange', ch)
    return () => {
      socket.off('updatePendingChange', ph)
      socket.off('connectionStateChange', ch)
    }
  }, [socket])
  const connected = conn === 'connected'
  return {
    holding: pending.active || !connected,
    count: pending.count,
    // 'updating' is the richer deploy message; otherwise we're just reconnecting.
    mode: pending.active ? 'updating' : 'reconnecting',
  }
}
