// The SendUserFile ingest (#3487). Its own module, like `worktreeHooks`, so it ships in
// the lazy user-facing card chunk rather than the entry bundle, and so the ~47 suites
// that hand-mock `@/api/hooks` never have to learn it.
import { useQuery } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import type { SentFilesIngest } from './types'

export const sentFilesKey = (sessionId: string, toolUseId: string) => ['sent-files', sessionId, toolUseId] as const

/** Retries per refusal: 503 `SENT_FILES_HISTORY_UNAVAILABLE` (the history scan ran
 *  out of time — the next request joins it) gets several; a 404 a couple, for the
 *  live race where the call's line is not on disk yet; anything else none. */
const RETRIES: Record<number, number> = { 503: 4, 404: 2 }

export function shouldRetrySentFiles(failureCount: number, error: unknown): boolean {
  const status = (error as Partial<ApiRequestError> | null)?.status
  return failureCount < (typeof status === 'number' ? RETRIES[status] ?? 0 : 0)
}

/** Backoff: 1 s, 2 s, 4 s, 8 s. */
export const sentFilesRetryDelay = (attempt: number) => Math.min(1000 * 2 ** attempt, 8000)

/**
 * The files a session's `SendUserFile` call `toolUseId` sent the user, each image
 * ingested on the daemon that owns the session (`POST /sessions/{id}/sent-files/{toolUseId}`).
 * A POST because it writes the attachment row, but idempotent server-side, so it is
 * cached like a read: one request per tool call, kept for the page's life once it
 * succeeds. A refusal is retried with backoff only where a later read can differ
 * (`shouldRetrySentFiles`), and a final one is not kept as an answer: the card's
 * next mount asks again. Until then the card keeps its notice.
 */
export function useSentFiles(sessionId: string | null, toolUseId: string, { enabled = true } = {}) {
  return useQuery<SentFilesIngest, ApiRequestError>({
    queryKey: sentFilesKey(sessionId ?? '', toolUseId),
    queryFn: () => api.post<SentFilesIngest>(
      `/sessions/${encodeURIComponent(sessionId ?? '')}/sent-files/${encodeURIComponent(toolUseId)}`),
    enabled: enabled && !!sessionId && !!toolUseId,
    retry: shouldRetrySentFiles,
    retryDelay: sentFilesRetryDelay,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  })
}
