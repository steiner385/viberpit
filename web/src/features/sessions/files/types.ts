// The worktree file list and file read (D-7), as served by `GET /api/sessions/{id}/files`
// and `GET /api/sessions/{id}/files/content`. The server half is
// `server/sessions/worktree_files.py`; the caps below mirror its `MAX_SCAN_ENTRIES` and
// `MAX_READ_BYTES` for the notices that name them.

export type SessionFileList = {
  /** Worktree-relative, `/`-separated; ranked by the server's fuzzy match, or most recent first for an empty query. */
  files: { path: string }[]
  /** Only the first `MAX_SCAN_ENTRIES` paths of the worktree were searched. */
  truncated: boolean
}

export type SessionFileContent = {
  path: string
  /** Null for a binary file. */
  text: string | null
  binary: boolean
  /** `text` is the first `MAX_READ_BYTES` of a larger file. */
  truncated: boolean
  /** The whole file's size in bytes. */
  size: number
}

export const MAX_SCAN_ENTRIES = 20_000
export const MAX_READ_BYTES = 512 * 1024
