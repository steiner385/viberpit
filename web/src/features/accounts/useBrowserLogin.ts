import { useMutation } from '@tanstack/react-query'
import { api, type ApiRequestError } from '@/api/client'
import type { ReauthState } from '@/api/types'

/** Sign an EXISTING Claude account in to claude.ai, so its sessions can be shown in
 *  the Claude apps (Remote Control). A code paste in the operator's own browser, like
 *  the setup-token re-auth: the state comes back `awaiting_code` with an `authUrl`, the
 *  code goes to `useSubmitReauthCode`, and progress is polled and cancelled through
 *  `useReauthStatus` / `useCancelReauth` (`/accounts/{name}/reauth`).
 *
 *  Lives in the accounts feature, not `@/api/hooks`, on purpose: that module is in
 *  the preloaded set and the initial-JS budget has no headroom, while this is only
 *  ever used from the lazily loaded accounts page. */
export function useBrowserLoginAccount() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (name) =>
      api.post<ReauthState>(`/accounts/${encodeURIComponent(name)}/browser-login`),
  })
}
