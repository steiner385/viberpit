import { useEffect, useState } from 'react'
import { getSessionSocket } from '@/ws/client'

const INACTIVE: { active: boolean; count: number } = { active: false, count: 0 }

function tryGetSocket() {
  try {
    return getSessionSocket()
  } catch (e) {
    // jsdom rejects relative WebSocket URLs ('/ws') with a SYNTAX_ERR DOMException, so the
    // hook degrades to INACTIVE under test. Re-throw anything else so a genuine runtime
    // error can't silently leave the banner dark in production.
    if (e instanceof DOMException && e.code === DOMException.SYNTAX_ERR) return null
    throw e
  }
}

export function useUpdatePending(): { active: boolean; count: number } {
  const socket = tryGetSocket()
  const [state, setState] = useState(socket ? socket.updatePendingState : INACTIVE)
  useEffect(() => {
    if (!socket) return
    const handler = (s: { active: boolean; count: number }) => setState(s)
    socket.on('updatePendingChange', handler)
    return () => socket.off('updatePendingChange', handler)
  }, [socket])
  return state
}
