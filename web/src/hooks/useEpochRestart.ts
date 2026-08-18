import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'

/**
 * Returns true while the client is recovering from a daemon epoch-change restart
 * (WS close code 1012). Fires immediately on the close event — before the next
 * /healthz poll — so the recovery banner appears in real time, not 10s late.
 *
 * Clears once the socket reconnects AND /healthz confirms the daemon is back.
 */
export function useEpochRestart(): boolean {
  const [recovering, setRecovering] = useState(false)

  useEffect(() => {
    let socket: ReturnType<typeof getSessionSocket>
    try {
      socket = getSessionSocket()
    } catch {
      return
    }

    const checkHealth = async () => {
      try {
        const res = await fetch('/healthz')
        if (!res.ok) return
        const body = (await res.json()) as { daemon?: string }
        if (body.daemon !== 'unavailable') setRecovering(false)
      } catch {
        // still down — stay in recovering state
      }
    }

    const onEpochRestart = () => setRecovering(true)
    const onStateChange = (state: ConnectionState) => {
      if (state === 'connected') void checkHealth()
    }

    socket.on('epochRestart', onEpochRestart)
    socket.on('connectionStateChange', onStateChange)

    return () => {
      socket.off('epochRestart', onEpochRestart)
      socket.off('connectionStateChange', onStateChange)
    }
  }, [])

  return recovering
}
