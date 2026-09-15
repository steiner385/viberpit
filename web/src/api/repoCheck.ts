/**
 * `POST /api/dispatch/repo-check` — "does this prompt look like it belongs to a
 * different repository than the one selected?"
 *
 * A plain function in its own module, NOT a hook in `hooks.ts`: ~47 test files
 * mock `@/api/hooks` wholesale, so every new export there breaks them.
 *
 * Fail-open: any error (network, 4xx/5xx, malformed body) is "no suggestion".
 */
import { api } from './client'
import type { RepoCheckResponse } from './types'

export async function checkRepo(prompt: string, repo: string | null): Promise<RepoCheckResponse | null> {
  try {
    return await api.post<RepoCheckResponse>('/dispatch/repo-check', { prompt, repo })
  } catch {
    return null
  }
}
