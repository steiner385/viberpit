/**
 * Best-effort "which repo is this?" label from a working-directory path, for the
 * archived list (archived rows carry `cwd`, not the live `repo` field). Feed the
 * result into formatRepoLabel(), which renders null/empty as the "Scratch" bucket.
 *
 * ccswitch worktrees live at `.ccswitch/worktrees/<repo>/<hash>`, so both the
 * basename (a meaningless hash) and the segment above `.ccswitch` (a shared
 * parent dir like `GitHub`) are wrong — we take the segment right after
 * `worktrees/`. Clones live at `.ccswitch/clones/<owner>/<repo>` and plain repo
 * dirs both end in the repo name, so those fall back to the path basename.
 */
export function repoFromCwd(cwd: string | null | undefined): string | null {
  const trimmed = cwd?.trim()
  if (!trimmed) return null
  const parts = trimmed.split('/').filter(Boolean)
  if (parts.length === 0) return null
  const ccIdx = parts.indexOf('.ccswitch')
  if (ccIdx >= 0 && parts[ccIdx + 1] === 'worktrees' && parts[ccIdx + 2]) {
    return parts[ccIdx + 2]
  }
  return parts[parts.length - 1]
}
