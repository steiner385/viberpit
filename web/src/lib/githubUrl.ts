/**
 * GitHub URL builders. Centralized so links to a committed artifact, a commit,
 * or a PR are constructed one way across the app (the Initiative board is the
 * first consumer — an artifact links to its file *at its SHA*, not at HEAD, so
 * the link keeps pointing at the reviewed bytes even after the branch moves on).
 *
 * `repo` is the "owner/name" slug ccswitch stores everywhere (e.g.
 * "steiner385/ccswitch").
 */

const BASE = 'https://github.com'

/** A file blob pinned at a specific ref (SHA/branch/tag). */
export function fileUrl(repo: string, ref: string, path: string): string {
  // Encode each path segment but keep the slashes that make up the tree path.
  const encodedPath = path.split('/').map(encodeURIComponent).join('/')
  return `${BASE}/${repo}/blob/${ref}/${encodedPath}`
}

/** A single commit. */
export function commitUrl(repo: string, sha: string): string {
  return `${BASE}/${repo}/commit/${sha}`
}

/** A pull request. */
export function prUrl(repo: string, number: number): string {
  return `${BASE}/${repo}/pull/${number}`
}
