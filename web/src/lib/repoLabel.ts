/**
 * Display label for "which repo is this session working on?", shown in the
 * dashboard sessions list (SessionCard).
 *
 * The API gives us a GitHub `nameWithOwner` string ("owner/name") for sessions
 * launched against a repo, or `null` / `undefined` for a scratch / arbitrary-cwd
 * session that isn't tied to any repo.
 *
 * Returns the text to render. `isScratch` lets the caller style the repo-less
 * case differently (dimmed), per the product decision.
 */
export interface RepoLabel {
  text: string
  isScratch: boolean
}

export function formatRepoLabel(repo: string | null | undefined): RepoLabel {
  const trimmed = repo?.trim()
  if (!trimmed) {
    return { text: 'Scratch', isScratch: true }
  }
  // Show the full "owner/name": the owner disambiguates same-named repos, and
  // SessionCard truncates + exposes the full value via a title tooltip, so long
  // names degrade gracefully on a narrow mobile list.
  return { text: trimmed, isScratch: false }
}
