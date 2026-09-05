/** The one-line "what is this tool call about?" summary.
 *
 * THE implementation. Two surfaces ask this question — the transcript's
 * `ToolCallCard` and the pending-approval `PermissionCard` — and they had a copy
 * each. They drifted the moment Codex arrived: the transcript copy learned to
 * read a Codex `Edit`'s `changes: [{path}]`, the approval copy did not, so the
 * card an operator has to *act* on was the one that said only "Edit".
 *
 * Every engine's tool input lands here, so the shapes are a union rather than a
 * per-engine branch:
 *
 * | engine | bash            | file tools                    |
 * |--------|-----------------|-------------------------------|
 * | Claude | `command`       | `file_path`                   |
 * | Codex  | `command`       | `changes: [{path, kind}]`     |
 * | Codex  | (approval)      | `grant_root` + `reason`       |
 *
 * A shape this does not recognize returns `''` — the caller then shows the bare
 * tool name, which is truthful. Guessing from an unknown payload is not.
 */
import type { ToolKind } from '@/session/events'

/** Bash commands are summarised, not quoted in full. */
const MAX_COMMAND_CHARS = 60

const FILE_KINDS: ReadonlySet<ToolKind> = new Set<ToolKind>(['edit', 'write', 'read'])

export function toolSummary(kind: ToolKind, input: Record<string, unknown>): string {
  if (kind === 'bash' && typeof input.command === 'string') {
    return input.command.slice(0, MAX_COMMAND_CHARS)
  }
  if (!FILE_KINDS.has(kind)) return ''
  if (typeof input.file_path === 'string' && input.file_path) return input.file_path
  const paths = changedPaths(input.changes)
  if (paths.length === 1) return paths[0]
  if (paths.length > 1) return `${paths[0]} +${paths.length - 1} more`
  // A Codex file-change APPROVAL describes the scope it wants rather than the
  // edit itself: there is no path list yet, because the write has not happened.
  if (typeof input.grant_root === 'string' && input.grant_root) return input.grant_root
  return ''
}

/** Paths out of a Codex `changes` array; empty for any other shape. */
export function changedPaths(changes: unknown): string[] {
  if (!Array.isArray(changes)) return []
  return changes
    .map((c) =>
      c != null && typeof c === 'object' && 'path' in c ? (c as { path: unknown }).path : null,
    )
    .filter((p): p is string => typeof p === 'string' && p !== '')
}
