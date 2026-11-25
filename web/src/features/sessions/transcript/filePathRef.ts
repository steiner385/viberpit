// A code span is treated as a file-path reference when it is a single
// whitespace-free token that either contains a path separator or ends in a
// filename with an extension, optionally suffixed with :line or :line:col.
// Kept deliberately strict to avoid linkifying prose, flags, or URLs.
import { KNOWN_FILE_EXTENSIONS } from '@/features/sessions/diff/pathLanguages'
import { isAttachmentUrl } from '@/lib/toolContent'

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
 * `path` as a cwd-relative, `/`-separated path when it names something under
 * `cwd`, else null — the form the Files tab reads, as the CLI resolves `@path`
 * against the session cwd (a subdirectory session included). An absolute path
 * must sit below `cwd` on a segment boundary (`/repo-evil/x` is not under
 * `/repo`); a relative one is taken as already cwd-relative. Any `..` or `~`
 * segment, the cwd itself and a trailing-slash (directory) path are refused.
 *
 * `isFilePathRef` is deliberately loose (it only gates click-to-copy), so a
 * LINK needs more: a relative token without a `/` must end in a known file
 * extension (`KNOWN_FILE_EXTENSIONS`) — `app.state.sessions`, `asyncio.sleep`
 * and `v2.1` are code and versions, not files. A `:` before the last segment is
 * a host:port (`localhost:8080/api`), and a `\` or a drive letter is a Windows
 * path; neither is ever a worktree file.
 *
 * A STRING check by design: the browser cannot realpath, and it only decides
 * whether a transcript path is offered as a Files-tab link. The server enforces
 * real containment (`worktree_rel`) when the file is read.
 */
export function worktreeRelativePath(path: string, cwd: string): string | null {
  if (!cwd.startsWith('/') || path.endsWith('/')) return null
  if (path.includes('\\') || /^[A-Za-z]:/.test(path)) return null
  const slash = path.lastIndexOf('/')
  if (path.slice(0, Math.max(slash, 0)).includes(':')) return null
  if (slash === -1) {
    const dot = path.lastIndexOf('.')
    if (dot <= 0 || !KNOWN_FILE_EXTENSIONS.has(path.slice(dot + 1).toLowerCase())) return null
  }
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

/**
 * A markdown link `href` or image `src` that names a file on the session host
 * rather than a URL: a scheme-less path (`docs/x.md`, `./shot.png`, `/abs/a.ts`)
 * or a `file://` URL (`file:///abs/a.ts`, host empty or `localhost`), parsed
 * into its decoded path and optional 1-based line — from a `#L10` / `#L10-L20`
 * fragment or a `:10[:4]` suffix. Any other fragment and a `?query` are dropped.
 *
 * Null for a `#fragment` (a footnote), a protocol-relative `//host` url, a
 * served `/api/attachments/{id}` url, and any other scheme (`https:`, `data:`,
 * `mailto:`, …). The browser can never open these paths itself — a relative
 * href resolves against the SPA, a `file:` one is blocked — so the caller
 * routes them to the Files tab or renders them as text.
 */
export function localPathTarget(url: string | null | undefined): FilePathRef | null {
  if (!url || url.startsWith('#') || url.startsWith('//') || isAttachmentUrl(url)) return null
  let raw = url
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url)
  if (scheme) {
    if (scheme[1].toLowerCase() !== 'file') return null
    let parsed: URL
    try { parsed = new URL(url) } catch { return null }
    if (parsed.host && parsed.host !== 'localhost') return null
    raw = parsed.pathname + parsed.hash
  }
  const hashAt = raw.indexOf('#')
  const fragment = hashAt === -1 ? '' : raw.slice(hashAt + 1)
  if (hashAt !== -1) raw = raw.slice(0, hashAt)
  const queryAt = raw.indexOf('?')
  if (queryAt !== -1) raw = raw.slice(0, queryAt)
  let path = raw
  try { path = decodeURIComponent(raw) } catch { /* a stray `%` stays literal */ }
  let line = 0
  const anchor = /^L(\d+)(?:-L?\d+)?$/.exec(fragment)
  const suffix = /:(\d+)(?::\d+)?$/.exec(path)
  if (anchor) line = Number(anchor[1])
  else if (suffix) {
    line = Number(suffix[1])
    path = path.slice(0, suffix.index)
  }
  if (path === '') return null
  return line > 0 ? { path, line } : { path }
}
