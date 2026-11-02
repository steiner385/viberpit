// The structured diff model, identical on server and client. The server half is
// `src/viberpit/worktree_diff.py`, served camelCase by `GET /api/sessions/{id}/diff`.
// Tool-call diffs reuse it through `toolDiff.ts`, so one renderer draws both.

export type DiffLine = {
  kind: 'context' | 'add' | 'del'
  /** Line number on the old side; null for an add, or when the diff is not anchored in a file. */
  oldNo: number | null
  /** Line number on the new side; null for a del, or when the diff is not anchored in a file. */
  newNo: number | null
  text: string
}

export type DiffHunk = {
  oldStart: number
  oldLines: number
  newStart: number
  newLines: number
  /** The section text after the `@@ … @@` range (e.g. the enclosing function), often empty. */
  header: string
  lines: DiffLine[]
}

export type FileDiff = {
  path: string
  oldPath: string | null
  status: 'added' | 'modified' | 'deleted' | 'renamed'
  additions: number
  deletions: number
  binary: boolean
  tooLarge: boolean
  hunks: DiffHunk[]
}

export type SessionDiff = {
  base: string | null
  head: string | null
  files: FileDiff[]
  additions: number
  deletions: number
  truncated: boolean
}
