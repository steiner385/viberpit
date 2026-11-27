import { useMutation } from '@tanstack/react-query'
import { api, type ApiRequestError } from '@/api/client'
import type { ReauthState } from '@/api/types'

/** Submit the pasted claude.ai code of the in-flight add. The server names the account
 *  (the one the add started for), so the client sends only the code.
 *
 *  Lives in the accounts feature, not `@/api/hooks`, for the same reason as
 *  `useBrowserLogin.ts`: that module is preloaded and the initial-JS budget has no
 *  headroom. */
export function useSubmitAddAccountCode() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (code) => api.post<ReauthState>('/accounts/add/code', { code }),
  })
}
