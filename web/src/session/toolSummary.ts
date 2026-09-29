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

/** "Tool: summary" — the one-line name a card, an aria-label or a decision echo
 *  uses for a tool call; the bare tool name when there is nothing to summarise. */
export function toolLabel(tool: string, kind: ToolKind, input: Record<string, unknown>): string {
  const summary = toolSummary(kind, input)
  return summary ? `${tool}: ${summary}` : tool
}

/** Cap on the raw-input fallback below: enough to read a request, never a dump. */
const MAX_DETAIL_CHARS = 2000

/**
 * The FULL request behind a permission card, for the operator to read before
 * deciding — where `toolSummary` is the one-line gist for a transcript row.
 * Bash: the whole command (the summary cuts it at 60 chars, which is how an
 * operator came to approve commands they could not see — 2026-09-29 critique,
 * P0). File tools: every path, one per line. Anything else with a non-empty
 * input: the input itself as JSON, so nothing is approved blind. `''` when the
 * tool name already says everything (empty input).
 */
export function toolDetail(kind: ToolKind, input: Record<string, unknown>): string {
  if (kind === 'bash' && typeof input.command === 'string') return input.command
  if (FILE_KINDS.has(kind)) {
    if (typeof input.file_path === 'string' && input.file_path) return input.file_path
    const paths = changedPaths(input.changes)
    if (paths.length > 0) return paths.join('\n')
    if (typeof input.grant_root === 'string' && input.grant_root) return input.grant_root
  }
  if (Object.keys(input).length === 0) return ''
  let json: string
  try {
    json = JSON.stringify(input, null, 1)
  } catch {
    return ''
  }
  return json.length > MAX_DETAIL_CHARS ? `${json.slice(0, MAX_DETAIL_CHARS)}\n…` : json
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
