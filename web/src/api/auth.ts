import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import { setDisplayReads } from './displayGate'

/** `GET /api/auth/me` (#1408). `display` is a kiosk's read-only principal. */
export interface AuthMe {
  identity: string
  scope: 'full' | 'display'
  authMode: string
  /** A display session only: the GET routes it may call, as anchored regex sources. */
  allowedReads?: string[]
}

/**
 * The caller's identity and scope — the one `/auth/me` query.
 *
 * The shell derives kiosk mode from `scope`, and quick replies key their cache
 * on `identity`. Never cached across a gap (`gcTime: 0`) and re-polled every
 * minute, so a sign-out or a revoked display link shows up without a reload. A display
 * session's `allowedReads` is handed to the request gate (`displayGate.ts`) here.
 */
export function useAuthMe() {
  return useQuery({
    queryKey: ['auth', 'me'],
    queryFn: async () => {
      const me = await api.get<AuthMe>('/auth/me')
      // The request gate (#3544) follows the server's list on every poll.
      setDisplayReads(me.scope === 'display' ? me.allowedReads : null)
      return me
    },
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 60_000,
    retry: false,
  })
}
