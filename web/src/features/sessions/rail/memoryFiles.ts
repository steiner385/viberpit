/**
 * How the Context card names a loaded memory file (D-13). The engine reports
 * each file as `{path, type, tokens}` with the CLI's own `type` and an absolute
 * path; this module is the ONE place that turns those into a label and a
 * shorter path.
 */

/** Label per CLI memory type. A type not listed here is shown exactly as the CLI sent it. */
const TYPE_LABEL: Readonly<Record<string, string>> = {
  User: 'User',
  Project: 'Project',
  Local: 'Local',
  Rule: 'Rule',
  Import: 'Import',
  Managed: 'Managed',
  AutoMem: 'AutoMem',
}

export function memoryTypeLabel(type: string): string {
  return TYPE_LABEL[type] ?? type
}

/**
 * The path as the card shows it: relative to the session's worktree when the
 * file is inside it, otherwise absolute. The browser does not know the host's
 * HOME, so a `~/…` form is not attempted — a guess would be wrong on any host
 * whose HOME is not `/home/<user>`.
 */
export function memoryDisplayPath(path: string, worktree: string | null | undefined): string {
  if (!worktree) return path
  const root = worktree.endsWith('/') ? worktree : `${worktree}/`
  return path.startsWith(root) && path.length > root.length ? path.slice(root.length) : path
}
