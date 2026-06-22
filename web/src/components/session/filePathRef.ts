// A code span is treated as a file-path reference when it is a single
// whitespace-free token that either contains a path separator or ends in a
// filename with an extension, optionally suffixed with :line or :line:col.
// Kept deliberately strict to avoid linkifying prose, flags, or URLs.
const LINE_SUFFIX = /(:\d+){0,2}$/

export function isFilePathRef(text: string): boolean {
  const token = text.trim()
  if (token === '' || /\s/.test(token)) return false
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(token)) return false // URL scheme (http://, etc.)

  const path = token.replace(LINE_SUFFIX, '')
  if (path === '') return false

  const last = path.slice(path.lastIndexOf('/') + 1)
  const hasExtension = /\.[A-Za-z0-9]+$/.test(last)
  const hasSeparator = path.includes('/') && /\/[^/]/.test(path) // a real segment after a slash

  // For paths with separators, we need at least one segment that looks like part of a path
  // (not just digits): starts with ., ~, /, or contains letters.
  if (hasSeparator) {
    const segments = path.split('/')
    const hasPathLike = segments.some((seg) => /[.~a-zA-Z]/.test(seg) || seg === '')
    if (!hasPathLike) return false
  }

  return hasExtension || hasSeparator
}
