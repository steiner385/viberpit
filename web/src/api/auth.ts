import { useQuery } from '@tanstack/react-query'
import { api } from './client'

/** `GET /api/auth/me` (#1408). `display` is a kiosk's read-only principal. */
export interface AuthMe {
  identity: string
  scope: 'full' | 'display'
  authMode: string
}

/**
 * The caller's identity and scope — the one `/auth/me` query.
 *
 * The shell derives kiosk mode from `scope`, and quick replies key their cache
 * on `identity`. Never cached across a gap (`gcTime: 0`) and re-polled every
 * minute, so a sign-out or a revoked display link shows up without a reload.
 */
export function useAuthMe() {
  return useQuery({
    queryKey: ['auth', 'me'],
    queryFn: () => api.get<AuthMe>('/auth/me'),
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 60_000,
    retry: false,
  })
}
