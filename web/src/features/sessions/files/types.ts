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

/** One line of a content search (D-20), `GET /api/sessions/{id}/files/search`. */
export type SessionFileMatch = {
  path: string
  /** 1-based. */
  line: number
  /** The line, clipped server-side to about 300 characters. */
  text: string
  /** Character offsets into `text` of the match; `end === start` (a regex search) marks nothing. */
  start: number
  end: number
}

export type SessionFileSearch = {
  matches: SessionFileMatch[]
  /** The server's match or file cap stopped the search early. */
  truncated: boolean
  /** The search ran out of time; `matches` is what it found before. */
  timedOut: boolean
}

/** The server's `MIN_SEARCH_QUERY_CHARS`: a shorter query is a 422, so it is never sent. */
export const MIN_CONTENT_QUERY_CHARS = 2
/** The content search box's debounce: each query runs `git grep` on the owning host. */
export const CONTENT_SEARCH_DEBOUNCE_MS = 250

export const MAX_SCAN_ENTRIES = 20_000
export const MAX_READ_BYTES = 512 * 1024
/** The memory-file read and user-memory size cap (D-13), mirroring `server/sessions/memory_files.py`
 *  `MAX_MEMORY_BYTES`: the viewer's truncation note and the editor's too-large copy name it. */
export const MAX_MEMORY_BYTES = 256 * 1024

/** A byte count as the notices name a cap: whole KiB, rounded up. */
export function kib(bytes: number): string {
  return `${Math.ceil(bytes / 1024).toLocaleString('en-US')} KiB`
}

/** The debounce on a files query typed by the user — the Files tab's search box and
 *  the composer's `@` menu ask the same endpoint at the same pace. */
export const FILE_SEARCH_DEBOUNCE_MS = 120
