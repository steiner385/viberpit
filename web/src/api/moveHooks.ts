// "Move to host…" (D-17): the dialog's preflight query and move mutation. Its own
// module so it ships in the lazy dialog chunk, not the entry bundle, and so the ~47
// suites that hand-mock `@/api/hooks` never have to learn these.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { QK } from './hooks'
import type { MovePreflight, MoveResult } from './types'

// Not under `['sessions']`: every sessions invalidation would re-run the preflight
// (and its GitHub visibility lookup) behind an open dialog.
export const movePreflightKey = (sessionId: string, host: string) =>
  ['move-preflight', sessionId, host] as const

/** What the dialog shows for `host` before posting: the verdict, the dirty paths,
 *  the repo's visibility and the background tasks that stop. Human-only (403 for
 *  tokens); it is fetched only once a host is chosen, and never retried — a refusal
 *  is an answer, not a blip. */
export function useMovePreflight(sessionId: string, host: string) {
  return useQuery<MovePreflight, ApiRequestError>({
    queryKey: movePreflightKey(sessionId, host),
    queryFn: () => api.get<MovePreflight>(
      `/sessions/${encodeURIComponent(sessionId)}/move/preflight?host=${encodeURIComponent(host)}`),
    enabled: host !== '',
    retry: false,
    staleTime: 0,
  })
}

export interface MoveVars {
  sessionId: string
  host: string
  carryDirty: boolean
  stopBackgroundTasks: boolean
}

/** Post the move. Synchronous on the server (push, fetch, resume — tens of seconds).
 *  The dialog renders every outcome inline, so the global toast is suppressed; every
 *  outcome refetches the sessions list, since a restored or failed move moves state too. */
export function useMoveSession() {
  const qc = useQueryClient()
  return useMutation<MoveResult, ApiRequestError, MoveVars>({
    mutationFn: ({ sessionId, host, carryDirty, stopBackgroundTasks }) =>
      api.post<MoveResult>(`/sessions/${encodeURIComponent(sessionId)}/move`,
        { host, carryDirty, stopBackgroundTasks }),
    meta: { suppressErrorToast: true },
    onSettled: (_data, _err, { sessionId }) => {
      void qc.invalidateQueries({ queryKey: QK.sessions })
      void qc.invalidateQueries({ queryKey: ['move-preflight', sessionId] })
    },
  })
}
