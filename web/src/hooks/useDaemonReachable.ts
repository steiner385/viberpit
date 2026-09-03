import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'

const POLL_INTERVAL_MS = 10_000

/**
 * Tracks whether the session daemon is reachable by polling /healthz.
 * Checks on every WebSocket reconnect (deploy restart always triggers one)
 * and on a 10-second interval while the daemon is known to be down.
 * Returns true once healthy (and on initial load before the first check).
 *
 * NOT the same hook as `useDaemonHealth` in `@/api/hooks` — that one is a
 * react-query hook returning `{ data: { available, … } }` and is mocked in
 * roughly a dozen test files. This one returns a plain `boolean` and backs
 * only `DaemonRestartBanner`. Do not merge them — they answer different
 * questions (reachability-by-polling vs. a queried health record).
 */
export function useDaemonReachable(): boolean {
  const [daemonOk, setDaemonOk] = useState(true)

  useEffect(() => {
    let intervalId: ReturnType<typeof setInterval> | null = null

    const check = async () => {
      try {
        const res = await fetch('/healthz')
        if (!res.ok) { setDaemonOk(false); return }
        const body = (await res.json()) as { daemon?: string }
        const ok = body.daemon !== 'unavailable'
        setDaemonOk(ok)
        if (ok && intervalId !== null) {
          clearInterval(intervalId)
          intervalId = null
        } else if (!ok && intervalId === null) {
          intervalId = setInterval(() => { void check() }, POLL_INTERVAL_MS)
        }
      } catch {
        setDaemonOk(false)
        if (intervalId === null) {
          intervalId = setInterval(() => { void check() }, POLL_INTERVAL_MS)
        }
      }
    }

    let socket: ReturnType<typeof getSessionSocket>
    try {
      socket = getSessionSocket()
    } catch {
      return
    }

    const onState = (state: ConnectionState) => { if (state === 'connected') void check() }
    socket.on('connectionStateChange', onState)

    return () => {
      socket.off('connectionStateChange', onState)
      if (intervalId !== null) clearInterval(intervalId)
    }
  }, [])

  return daemonOk
}
