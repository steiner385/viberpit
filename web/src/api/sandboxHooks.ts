// The sandbox (D-23): the Environment card's facts query and the toggle. Their own module,
// like `worktreeHooks`, so they ship in the lazy chunks that use them (the rail and the
// hold actions) rather than the entry bundle, and so the ~47 suites that hand-mock
// `@/api/hooks` never have to learn them.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { QK } from './hooks'
import type { SandboxFacts, SandboxToggleResult } from './types'

// Not under `['sessions']`: every sessions invalidation would re-run the host preflight read.
export const sandboxFactsKey = (sessionId: string) => ['sandbox-facts', sessionId] as const

const path = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}/sandbox`

/**
 * What the toggle may do (`GET /sessions/{id}/sandbox`, on the owning daemon). The host
 * preflight is cached there, so a read is cheap; it is kept a minute here and never
 * retried — a refusal (404, `daemon_outdated`) is an answer, and the row hides. Only
 * asked for where the engine has the capability (`enabled`).
 */
export function useSandboxFacts(sessionId: string, { enabled = true } = {}) {
  return useQuery<SandboxFacts, ApiRequestError>({
    queryKey: sandboxFactsKey(sessionId),
    queryFn: () => api.get<SandboxFacts>(path(sessionId)),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })
}

/**
 * `POST /sessions/{id}/sandbox {enabled}` — person-only; both directions respawn the
 * session. On a held session `true` is Retry and `false` is "Turn off sandbox and
 * continue". The caller renders a refusal inline, so the global toast is suppressed;
 * either outcome refetches the list (the hold and the state ride the session payload)
 * and the facts.
 */
export function useSetSandbox() {
  const qc = useQueryClient()
  return useMutation<SandboxToggleResult, ApiRequestError, { id: string; enabled: boolean }>({
    mutationFn: ({ id, enabled }) => api.post<SandboxToggleResult>(path(id), { enabled }),
    meta: { suppressErrorToast: true },
    onSettled: (_data, _err, { id }) => {
      void qc.invalidateQueries({ queryKey: QK.sessions })
      void qc.invalidateQueries({ queryKey: sandboxFactsKey(id) })
    },
  })
}
