import { REPO_NAMESPACES } from '@/brand'

/**
 * Best-effort "which repo is this?" label from a working-directory path, for the
 * archived list (archived rows carry `cwd`, not the live `repo` field). Feed the
 * result into formatRepoLabel(), which renders null/empty as the "Scratch" bucket.
 *
 * ViberPit worktrees live at `<namespace>/worktrees/<repo>/<hash>` (namespace:
 * either spelling in `REPO_NAMESPACES`), so both the basename (a meaningless
 * hash) and the segment above the namespace (a shared parent dir like `GitHub`)
 * are wrong — we take the segment right after `worktrees/`. Clones live at
 * `<namespace>/clones/<owner>/<repo>` and plain repo dirs both end in the repo
 * name, so those fall back to the path basename.
 */
export function repoFromCwd(cwd: string | null | undefined): string | null {
  const trimmed = cwd?.trim()
  if (!trimmed) return null
  const parts = trimmed.split('/').filter(Boolean)
  if (parts.length === 0) return null
  const nsIdx = parts.findIndex((p) => (REPO_NAMESPACES as readonly string[]).includes(p))
  if (nsIdx >= 0 && parts[nsIdx + 1] === 'worktrees' && parts[nsIdx + 2]) {
    return parts[nsIdx + 2]
  }
  return parts[parts.length - 1]
}
