import { buildLines } from '@/lib/toolContent'
import { changedPaths } from '@/session/toolSummary'
import type { DiffLine, FileDiff } from './types'

/**
 * An Edit/Write tool call as a `FileDiff`, so the transcript draws it with the
 * same `DiffRenderer` as the Changes pane.
 *
 * The lines come from `buildLines` — the diff lives in the call's `input`
 * (Edit: old_string/new_string, Write: content); the result payload only says
 * whether it applied. A tool call never says WHERE in the file the edit landed,
 * so every line number is null rather than an invented 1..n.
 *
 * Returns null when the input carries no text: a Codex file change reports
 * which files changed and how, never their content.
 */
export function toolInputToFileDiff(input: Record<string, unknown>): FileDiff | null {
  const built = buildLines(input)
  if (built.length === 0) return null
  const lines: DiffLine[] = built.map((l) => ({
    kind: l.prefix === '-' ? 'del' : 'add',
    oldNo: null,
    newNo: null,
    text: l.text,
  }))
  const additions = lines.filter((l) => l.kind === 'add').length
  const deletions = lines.length - additions
  return {
    path: typeof input.file_path === 'string' ? input.file_path : '',
    oldPath: null,
    status: 'modified',
    additions,
    deletions,
    binary: false,
    tooLarge: false,
    hunks: [{ oldStart: 0, oldLines: deletions, newStart: 0, newLines: additions, header: '', lines }],
  }
}

/** One entry of a Codex file change (`changes: [{path, kind}]`). */
export interface FileChange {
  path: string
  kind: string | null
}

/** `changes: [{path, kind}]` as a typed list; empty for any other shape. */
export function fileChanges(input: Record<string, unknown>): FileChange[] {
  const paths = changedPaths(input.changes)
  if (paths.length === 0) return []
  const raw = Array.isArray(input.changes) ? input.changes : []
  return paths.map((path, i) => {
    const entry = raw[i]
    const kind =
      entry != null && typeof entry === 'object' && 'kind' in entry
        ? (entry as { kind: unknown }).kind
        : null
    return { path, kind: typeof kind === 'string' ? kind : null }
  })
}
