import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'

export interface CredentialAlert {
  repo: string
  since: number
  error: string
  settingsUrl: string
}

interface CredentialAlertsResponse {
  alerts: CredentialAlert[]
}

const QUERY_KEY = ['credential-alerts'] as const

/**
 * Polls the daemon for repos whose ccswitch clone can't fetch because its git
 * credentials are stale (issue #670). The list auto-clears server-side once a
 * fetch succeeds, so the banner disappears on its own when credentials recover
 * — no client bookkeeping. Gated to production so dev/test never poll.
 */
export function useCredentialAlerts(opts: { enabled?: boolean } = {}) {
  const enabled = opts.enabled ?? import.meta.env.PROD
  return useQuery<CredentialAlert[]>({
    queryKey: QUERY_KEY,
    queryFn: async () => {
      const res = await api.get<CredentialAlertsResponse>('/github/credential-alerts')
      return res.alerts ?? []
    },
    refetchInterval: 30_000,
    enabled,
  })
}

/**
 * Dismisses one repo's alert. Advisory: if the credential is still broken the
 * next failed fetch re-raises it. Refetches the list so the banner reconciles.
 */
export function useDismissCredentialAlert() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (repo: string) =>
      api.del(`/github/credential-alerts?repo=${encodeURIComponent(repo)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }),
  })
}
