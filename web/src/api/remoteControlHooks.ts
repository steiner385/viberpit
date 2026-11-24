// Remote Control: the Environment card's toggle. Its own module, like `sandboxHooks`, so it
// ships in the lazy rail chunk rather than the entry bundle, and so the ~47 suites that
// hand-mock `@/api/hooks` never have to learn it.
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { patchSessionInCache } from './hooks'
import type { RemoteControlResult } from './types'

/**
 * `POST /sessions/{id}/remote-control {enabled}` — sets the session's override (it stops
 * following the Settings default) and applies it live, no respawn. The caller renders a
 * refusal inline, so the global toast is suppressed. The answer lands in the cached list
 * (effective value, override and link), then the list refetches so the server's copy wins.
 */
export function useSetRemoteControl() {
  const qc = useQueryClient()
  return useMutation<RemoteControlResult, ApiRequestError, { id: string; enabled: boolean }>({
    mutationFn: ({ id, enabled }) =>
      api.post<RemoteControlResult>(`/sessions/${encodeURIComponent(id)}/remote-control`, { enabled }),
    meta: { suppressErrorToast: true },
    onSuccess: ({ enabled, url }, { id }) => patchSessionInCache(qc, id, {
      remoteControl: enabled, remoteControlOverride: enabled, remoteControlUrl: url,
    }),
  })
}
