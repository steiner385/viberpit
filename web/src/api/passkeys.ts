/**
 * The caller's passkeys as React Query hooks (#1410): list, enrol, revoke.
 * The ceremonies themselves are in `webauthn.ts`.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import { enrolPasskey, type PasskeyCredential } from './webauthn'

const CREDENTIALS_KEY = ['auth', 'credentials'] as const

/**
 * The caller's unrevoked passkeys. Mount only once passkeys are known to be on
 * (`/api/config.webauthnEnabled`): the route 404s while they are off.
 */
export function usePasskeys() {
  return useQuery({
    queryKey: CREDENTIALS_KEY,
    queryFn: () => api.get<PasskeyCredential[]>('/auth/credentials'),
  })
}

export function useEnrolPasskey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (label: string) => enrolPasskey(label),
    onSuccess: () => qc.invalidateQueries({ queryKey: CREDENTIALS_KEY }),
  })
}

/** Revoking a passkey also ends every session it signed in. */
export function useRevokePasskey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del<void>(`/auth/credentials/${encodeURIComponent(id)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: CREDENTIALS_KEY }),
  })
}
