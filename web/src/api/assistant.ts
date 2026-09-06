// web/src/api/assistant.ts
//
// The AI assistant's status + session lifecycle, fetched/posted in ONE place —
// colocated hooks pattern (see fleet.ts). Mirrors server/assistant/api.py's two
// endpoints exactly: GET /assistant/status and POST /assistant/session.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { ApiRequestError } from '@/api/client'

/** The query key, exported so invalidations cannot misspell it. */
export const ASSISTANT_STATUS_KEY = ['assistant', 'status'] as const

export interface AssistantStatus {
  enabled: boolean
  sessionId: string | null
  model: string
}

export interface AssistantSessionInput {
  fresh?: boolean
  model?: string
}

export interface AssistantSession {
  id: string
  model: string
  state: 'created' | 'resumed' | 'existing'
}

/** Whether the assistant is enabled, and its current (if any) session + model. */
export function useAssistantStatus() {
  return useQuery<AssistantStatus>({
    queryKey: ASSISTANT_STATUS_KEY,
    queryFn: () => api.get<AssistantStatus>('/assistant/status'),
    staleTime: 30_000,
  })
}

/**
 * Create, resume, or reset the assistant session. Invalidates the status
 * query on success so the drawer picks up the new sessionId/model.
 */
export function useAssistantSession() {
  const qc = useQueryClient()
  return useMutation<AssistantSession, ApiRequestError, AssistantSessionInput>({
    mutationFn: (input) => api.post<AssistantSession>('/assistant/session', input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ASSISTANT_STATUS_KEY })
    },
  })
}

/**
 * Switch the assistant's own session model.
 *
 * Posts to the ordinary session model route — the assistant IS a session — and the
 * server refuses a gated tier with 400 MODEL_GATED for `kind == assistant` rather
 * than parking it, because a parked assistant is a drawer that never answers again.
 */
export function useSetAssistantModel() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, { sessionId: string; model: string }>({
    mutationFn: ({ sessionId, model }) =>
      api.post<void>(`/sessions/${sessionId}/model`, { model }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ASSISTANT_STATUS_KEY })
    },
  })
}

/**
 * Reset the assistant to a fresh session — always forces `fresh: true`,
 * regardless of what the caller passes.
 */
export function useResetAssistant() {
  const mutation = useAssistantSession()
  return {
    ...mutation,
    mutateAsync: (input: AssistantSessionInput = {}) =>
      mutation.mutateAsync({ ...input, fresh: true }),
  }
}
