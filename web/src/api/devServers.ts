// The session's detected dev servers (D-18). SessionBody owns the one query: its
// answer lists the Browser tab even on a host without X11, and the tab's "Dev
// servers" section renders the same result as a prop — so the lazy Browser tab chunk
// never imports this module and no shared chunk is split off the entry path. The Open
// mutation lives in the lazy `devServerHooks.ts`. Neither is in `hooks.ts`, so the
// ~47 suites that hand-mock `@/api/hooks` never have to learn them.
import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { DevServer, DevServers } from './types'

/** The server caches detection for 3 s per session; polling faster buys nothing. */
export const DEV_SERVERS_POLL_MS = 5_000

/** What the Browser tab renders: the detected servers, or `null` for no section at
 *  all (not answered yet, detection switched off, or a host that never could say). */
export interface SessionDevServers {
  servers: DevServer[] | null
  refetch: () => void
}

/**
 * The session's dev servers. Fetched when the session opens and again when a turn
 * ends; polled only while the Browser tab is actually shown — `?view=browser` and
 * the tab listed (X11 on, or a server already detected) — and the page is visible:
 * React Query's interval never runs in the background. A GET that finds servers
 * costs the edge a `tailscale whois`, so nothing polls behind the user's back.
 */
export function useSessionDevServers(
  sessionId: string, busy: boolean, requested: boolean, x11: boolean,
): SessionDevServers {
  const { data, refetch } = useQuery({
    queryKey: ['dev-servers', sessionId],
    queryFn: () => api.get<DevServers>(`/sessions/${encodeURIComponent(sessionId)}/dev-servers`),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (q) =>
      requested && (x11 || (q.state.data?.servers?.length ?? 0) > 0) ? DEV_SERVERS_POLL_MS : false,
  })
  // Re-ask whenever the session goes idle (a turn ended), and twice more shortly
  // after — `npm run dev &` is often still starting when the turn ends. Bounded, not
  // a poll. On mount and on a session switch the first joins the query's own
  // in-flight fetch rather than adding one.
  useEffect(() => {
    if (busy) return
    const again = () => void refetch({ cancelRefetch: false })
    again()
    const timers = [5e3, 15e3].map((ms) => setTimeout(again, ms))
    return () => timers.forEach(clearTimeout)
  }, [busy, refetch])
  return {
    // A failed re-poll keeps the last answer; a host that never answered has none.
    servers: data?.enabled !== false && Array.isArray(data?.servers) ? data.servers : null,
    refetch,
  }
}
