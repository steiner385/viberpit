import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'

/**
 * A row of `GET /api/assistant/tokens` (#1408). Display links are the
 * `scope: "display"` rows of the same table as the assistant's `ccas2:`
 * tokens — one list/revoke implementation, filtered here by scope.
 */
export interface AssistantTokenRow {
  id: string
  scope: 'read' | 'act' | 'display'
  label: string | null
  createdAt: string
  expiresAt: string
  revokedAt: string | null
  lastUsedAt: string | null
}

/** The mint answer: the row plus the secret, which the server never returns again. */
export interface MintedToken extends AssistantTokenRow { token: string }

const TOKENS_KEY = ['assistant', 'tokens'] as const

/** Unrevoked display links, newest first (the server's order). */
export function useDisplayTokens() {
  return useQuery({
    queryKey: TOKENS_KEY,
    queryFn: () => api.get<AssistantTokenRow[]>('/assistant/tokens'),
    select: (rows) => rows.filter((r) => r.scope === 'display' && r.revokedAt === null),
  })
}

export function useMintDisplayToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { label: string; ttlDays: number }) =>
      api.post<MintedToken>('/assistant/tokens', { scope: 'display', ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: TOKENS_KEY }),
  })
}

export function useRevokeToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del<{ ok: boolean }>(`/assistant/tokens/${encodeURIComponent(id)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: TOKENS_KEY }),
  })
}
