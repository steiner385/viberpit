// A code span is treated as a file-path reference when it is a single
// whitespace-free token that either contains a path separator or ends in a
// filename with an extension, optionally suffixed with :line or :line:col.
// Kept deliberately strict to avoid linkifying prose, flags, or URLs.
const LINE_SUFFIX = /(:\d+){0,2}$/

export function isFilePathRef(text: string): boolean {
  // Whitespace anywhere — including a trailing newline — disqualifies. Checked
  // on the RAW input before any trim: a language-less fenced code block keeps a
  // plain-string child with a trailing "\n", and that newline is the only
  // signal distinguishing it from a genuine inline code span. Trimming first
  // would erase that signal and let block code leak through as a copy target.
  if (text === '' || /\s/.test(text)) return false
  const token = text
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(token)) return false // URL scheme (http://, etc.)

  const path = token.replace(LINE_SUFFIX, '')
  if (path === '') return false

  const last = path.slice(path.lastIndexOf('/') + 1)
  const hasExtension = /\.[A-Za-z0-9]+$/.test(last)
  const hasSeparator = path.includes('/') && /\/[^/]/.test(path) // a real segment after a slash

  // For paths with separators, require at least one segment that looks like part
  // of a path (starts with ./~, contains letters, or is an empty boundary
  // segment) — not purely digits. This rejects ratios like "1/2" and
  // intentionally supersedes the plan's Task 1 draft, which lacked this guard
  // and would have accepted "1/2".
  if (hasSeparator) {
    const segments = path.split('/')
    const hasPathLike = segments.some((seg) => /[.~a-zA-Z]/.test(seg) || seg === '')
    if (!hasPathLike) return false
  }

  return hasExtension || hasSeparator
}

/** A transcript path reference split into the path and its optional 1-based `:line[:col]`. */
export interface FilePathRef {
  path: string
  line?: number
  col?: number
}

/**
 * `isFilePathRef`'s token, parsed: `src/a.ts:12:4` → `{path: 'src/a.ts', line: 12, col: 4}`.
 * Null for anything `isFilePathRef` rejects. A `:0` line or col reads as absent.
 */
export function parseFilePathRef(text: string): FilePathRef | null {
  if (!isFilePathRef(text)) return null
  const suffix = text.match(LINE_SUFFIX)?.[0] ?? ''
  const [line, col] = suffix.split(':').slice(1).map(Number)
  const ref: FilePathRef = { path: text.slice(0, text.length - suffix.length) }
  if (line > 0) {
    ref.line = line
    if (col > 0) ref.col = col
  }
  return ref
}

/**
 * `path` as a worktree-relative, `/`-separated path when it names something
 * under `cwd`, else null. An absolute path must sit below `cwd` on a segment
 * boundary (`/repo-evil/x` is not under `/repo`); a relative one is taken as
 * already worktree-relative. Any `..` or `~` segment, the cwd itself and a
 * trailing-slash (directory) path are refused.
 *
 * A STRING check by design: the browser cannot realpath, and it only decides
 * whether a transcript path is offered as a Files-tab link. The server enforces
 * real containment (`worktree_rel`) when the file is read.
 */
export function worktreeRelativePath(path: string, cwd: string): string | null {
  if (!cwd.startsWith('/') || path.endsWith('/')) return null
  let rest = path
  if (path.startsWith('/')) {
    const root = cwd.replace(/\/+$/, '')
    if (!path.startsWith(`${root}/`)) return null
    rest = path.slice(root.length + 1)
  }
  const segments = rest.split('/').filter((s) => s !== '' && s !== '.')
  if (segments.length === 0 || segments.some((s) => s === '..' || s.startsWith('~'))) return null
  return segments.join('/')
}
