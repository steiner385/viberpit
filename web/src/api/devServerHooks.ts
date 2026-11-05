// The Browser tab's Open-in-VNC mutation (D-18). Its own module so it ships in the
// lazy Browser tab chunk, not the entry bundle (the list itself is `devServers.ts`).
import { useMutation } from '@tanstack/react-query'
import { api, apiErrorCode, type ApiRequestError } from './client'
import type { DevServerOpened } from './types'

/** Open a detected port in the session's virtual display (the `vnc` route). The row
 *  renders every outcome, so the global toast is suppressed; a port that is no
 *  longer detected calls `onGone` so the list re-polls. */
export function useOpenDevServer(sessionId: string, onGone: () => void) {
  return useMutation<DevServerOpened, ApiRequestError, number>({
    mutationFn: (port) =>
      api.post<DevServerOpened>(`/sessions/${encodeURIComponent(sessionId)}/dev-servers/${port}/open`),
    meta: { suppressErrorToast: true },
    onError: (err) => {
      if (apiErrorCode(err) === 'DEV_SERVER_NOT_DETECTED') onGone()
    },
  })
}
