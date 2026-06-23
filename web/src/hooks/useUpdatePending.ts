import { useEffect, useState } from 'react'
import { getSessionSocket } from '@/ws/client'

const INACTIVE: { active: boolean; count: number } = { active: false, count: 0 }

function tryGetSocket() {
  try {
    return getSessionSocket()
  } catch {
    // In test environments WebSocket may not accept relative URLs; return null.
    return null
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
