// Worktree Discard / Repair (D-19): the preflight query and the two mutations. Its own
// module so it ships in the lazy rail chunk, not the entry bundle, and so the ~47
// suites that hand-mock `@/api/hooks` never have to learn these.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { QK } from './hooks'
import type { SessionRecoveryRefs, WorktreeDiscardResult, WorktreePreflight, WorktreeRepairResult } from './types'

// Not under `['sessions']`: every sessions invalidation would re-run the git reads.
export const worktreePreflightKey = (sessionId: string) => ['worktree-preflight', sessionId] as const

const path = (sessionId: string, leaf: string) => `/sessions/${encodeURIComponent(sessionId)}/worktree/${leaf}`

/**
 * What Discard and Repair would do (`git status` + an unpushed count on the owning
 * daemon). Person-only: a token caller's 403 is an answer — the controls hide — so
 * it is never retried. The card asks only while the session is idle (`enabled`) and
 * keeps an answer a minute, so a turn starting or ending costs the daemon nothing;
 * `fresh` (the Discard dialog) always re-reads on open rather than trusting it.
 */
export function useWorktreePreflight(sessionId: string, { enabled = true, fresh = false } = {}) {
  return useQuery<WorktreePreflight, ApiRequestError>({
    queryKey: worktreePreflightKey(sessionId),
    queryFn: () => api.get<WorktreePreflight>(path(sessionId, 'preflight')),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
    ...(fresh ? { refetchOnMount: 'always' as const } : {}),
  })
}

/** The mutations render their own failure (dialog / toast), so the global toast is
 *  suppressed; either outcome refetches the list (a discard archives the session).
 *  Callers read the outcome from `mutateAsync`'s promise, not `mutate`'s callbacks:
 *  React Query drops those once the caller unmounts, and a discard's archive can
 *  unmount the card mid-request. */
function useWorktreeMutation<T>(leaf: 'discard' | 'repair', body?: unknown) {
  const qc = useQueryClient()
  return useMutation<T, ApiRequestError, string>({
    mutationFn: (sessionId) => api.post<T>(path(sessionId, leaf), body),
    meta: { suppressErrorToast: true },
    onSettled: (_data, _err, sessionId) => {
      void qc.invalidateQueries({ queryKey: QK.sessions })
      void qc.invalidateQueries({ queryKey: worktreePreflightKey(sessionId) })
    },
  })
}

/** End the session and remove its worktree, after the recovery refs are written. */
export const useDiscardWorktree = () => useWorktreeMutation<WorktreeDiscardResult>('discard', { confirm: true })

/** Recreate a missing or empty managed worktree from its branch. */
export const useRepairWorktree = () => useWorktreeMutation<WorktreeRepairResult>('repair')

/**
 * Where a discarded session's work went, for the archived reader: read from the
 * discard's audit row, so it outlives the toast and the page. `recoveryRefs` is empty
 * for a session that was never discarded (or had nothing to save).
 */
export function useSessionRecoveryRefs(sessionId: string) {
  return useQuery<SessionRecoveryRefs, ApiRequestError>({
    queryKey: ['session-recovery-refs', sessionId],
    queryFn: () => api.get<SessionRecoveryRefs>(`/sessions/${encodeURIComponent(sessionId)}/recovery-refs`),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })
}
