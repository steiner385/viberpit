// The SendUserFile ingest (#3487). Its own module, like `worktreeHooks`, so it ships in
// the lazy user-facing card chunk rather than the entry bundle, and so the ~47 suites
// that hand-mock `@/api/hooks` never have to learn it.
import { useQuery } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import type { SentFilesIngest } from './types'

export const sentFilesKey = (sessionId: string, toolUseId: string) => ['sent-files', sessionId, toolUseId] as const

/**
 * The files a session's `SendUserFile` call `toolUseId` sent the user, each image
 * ingested on the daemon that owns the session (`POST /sessions/{id}/sent-files/{toolUseId}`).
 * A POST because it writes the attachment row, but idempotent server-side, so it is
 * cached like a read: one request per tool call, kept for the page's life. Any refusal
 * (404 no such call / session, 409 `daemon_outdated`) is an answer — the card keeps
 * its notice — so it is never retried.
 */
export function useSentFiles(sessionId: string | null, toolUseId: string, { enabled = true } = {}) {
  return useQuery<SentFilesIngest, ApiRequestError>({
    queryKey: sentFilesKey(sessionId ?? '', toolUseId),
    queryFn: () => api.post<SentFilesIngest>(
      `/sessions/${encodeURIComponent(sessionId ?? '')}/sent-files/${encodeURIComponent(toolUseId)}`),
    enabled: enabled && !!sessionId && !!toolUseId,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  })
}
