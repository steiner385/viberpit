import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'
import { isOutdated, BUILD_SHA } from '@/lib/version'

interface Options {
  enabled?: boolean
  buildSha?: string
}

/**
 * Watches for a newer deployed frontend. On every WebSocket reconnect (a deploy always restarts
 * the edge → reconnect), fetches /healthz and compares the running sha to this bundle's build sha.
 * Returns a sticky `updateAvailable`. Reconnect-triggered only; gated to production so dev/test
 * never fetch or flip. `enabled`/`buildSha` are seams for testing.
 */
export function useVersionWatch(opts: Options = {}): boolean {
  const enabled = opts.enabled ?? import.meta.env.PROD
  const buildSha = opts.buildSha ?? BUILD_SHA
  const [updateAvailable, setUpdateAvailable] = useState(false)

  useEffect(() => {
    if (!enabled) return
    let socket: ReturnType<typeof getSessionSocket>
    try {
      socket = getSessionSocket()
    } catch {
      return // jsdom rejects '/ws'; no socket → nothing to watch
    }
    const check = async () => {
      try {
        const res = await fetch('/healthz')
        if (!res.ok) return
        const body = (await res.json()) as { sha?: string }
        if (isOutdated(buildSha, body.sha)) setUpdateAvailable(true)
      } catch {
        // unreachable / parse error — re-check on the next reconnect
      }
    }
    const onState = (state: ConnectionState) => { if (state === 'connected') void check() }
    socket.on('connectionStateChange', onState)
    return () => socket.off('connectionStateChange', onState)
  }, [enabled, buildSha])

  return updateAvailable
}
