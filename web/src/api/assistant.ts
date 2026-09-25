// web/src/api/assistant.ts
//
// The AI assistant's status + session lifecycle, fetched/posted in ONE place —
// colocated hooks pattern (see fleet.ts). Mirrors server/assistant/api.py's
// endpoints: GET /assistant/status, POST /assistant/session, GET
// /assistant/history, POST /assistant/session/{id}/resume and GET
// /assistant/gaps.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { ApiRequestError } from '@/api/client'

/** The query key, exported so invalidations cannot misspell it. */
export const ASSISTANT_STATUS_KEY = ['assistant', 'status'] as const

/** The history list's query key, exported for the same reason. */
export const ASSISTANT_HISTORY_KEY = ['assistant', 'history'] as const

export interface AssistantStatus {
  enabled: boolean
  sessionId: string | null
  model: string
  /** Whether the operator tier (deploy, host upgrade, account refresh/enable/reauth)
   * is on — server/assistant/policy.py:operator_tools_enabled, mirrored regardless of
   * whether an assistant session exists yet. */
  operatorTools: boolean
  /** Which agent engine answers this drawer — `claude_code` or `codex`
   * (VIBERPIT_ASSISTANT_ENGINE). A running thread reports its OWN engine, which can
   * differ from the deployment default after the setting is changed. */
  engine: string
}

export interface AssistantSessionInput {
  fresh?: boolean
  model?: string
}

/** Body for POST /assistant/feedback — a thumbs up/down on one assistant turn. */
export interface AssistantFeedbackInput {
  sessionId: string
  turnId: string
  vote: 'up' | 'down'
  note?: string
}

export interface AssistantSession {
  id: string
  model: string
  engine: string
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

/** One row of the drawer's "History" list — an archived assistant thread owned
 * by the caller. `snippet` is `initial_prompt` (the same human-readable-goal
 * field other session surfaces already reuse) and is `null` for a thread that
 * never got one; `archivedAt` is `null` only for a row the server could not
 * timestamp at all. */
export interface AssistantHistoryEntry {
  id: string
  snippet: string | null
  archivedAt: string | null
}

/**
 * The caller's own archived assistant threads, most-recently-archived first
 * (server caps at 20, no pagination). `enabled` gates the fetch — the history
 * list is a popover, not always-visible chrome, so it only needs to fetch
 * while open.
 */
export function useAssistantHistory(enabled: boolean) {
  return useQuery<AssistantHistoryEntry[]>({
    queryKey: ASSISTANT_HISTORY_KEY,
    queryFn: () => api.get<AssistantHistoryEntry[]>('/assistant/history'),
    enabled,
    staleTime: 15_000,
  })
}

/**
 * Resume an archived thread from the History list as the live one. Invalidates
 * BOTH the status query (so the drawer picks up the new live sessionId — the
 * same mechanism `useAssistantSession` already relies on) and the history query
 * (the resumed thread must drop off the list, and the thread it displaced must
 * appear on it).
 */
export function useResumeAssistantSession() {
  const qc = useQueryClient()
  return useMutation<AssistantSession, ApiRequestError, string>({
    mutationFn: (sessionId) => api.post<AssistantSession>(`/assistant/session/${sessionId}/resume`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ASSISTANT_STATUS_KEY })
      void qc.invalidateQueries({ queryKey: ASSISTANT_HISTORY_KEY })
    },
  })
}

/** One capability users asked the assistant for that no tool could do — the
 * `assistant.gap` audit rows (its `report_gap` tool), grouped by capability.
 * `lastSeen` is an ISO UTC timestamp. */
export interface AssistantGap {
  capability: string
  count: number
  lastSeen: string
}

/** GET /assistant/gaps — gaps in the last `days` days, most-reported first. */
export interface AssistantGapsReport {
  days: number
  total: number
  gaps: AssistantGap[]
}

/** The gaps report's query key, per window. */
export const assistantGapsKey = (days: number) => ['assistant', 'gaps', days] as const

/** The assistant's reported capability gaps over the last `days` days. */
export function useAssistantGaps(days = 30) {
  return useQuery<AssistantGapsReport>({
    queryKey: assistantGapsKey(days),
    queryFn: () => api.get<AssistantGapsReport>(`/assistant/gaps?days=${days}`),
    staleTime: 5 * 60_000,
  })
}

/**
 * Record a thumbs up/down on one assistant turn (TurnFeedback, in the transcript).
 * Fire-and-forget: the response carries nothing the caller needs and no cached
 * query depends on this data, so there is no result type to capture and nothing
 * to invalidate on success.
 */
export function useAssistantFeedback() {
  return useMutation<void, ApiRequestError, AssistantFeedbackInput>({
    mutationFn: (input) => api.post<void>('/assistant/feedback', input),
  })
}
