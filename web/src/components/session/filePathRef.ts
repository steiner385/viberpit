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
