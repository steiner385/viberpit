// File path → highlight.js language, and the file extensions the app recognises
// as source/doc files. A plain-data leaf (no highlight.js import) so the
// transcript's path links (`transcript/filePathRef`) can read the same map the
// diff highlighter uses without pulling the highlighter in. Every language value
// must be registered in `diffHighlight.ts` (a test holds the two together).
const BY_EXTENSION: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', mts: 'typescript', cts: 'typescript',
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  py: 'python', pyi: 'python',
  json: 'json',
  css: 'css', scss: 'scss',
  html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  yml: 'yaml', yaml: 'yaml',
  md: 'markdown',
  go: 'go',
  rs: 'rust',
  sql: 'sql',
  toml: 'ini', ini: 'ini', cfg: 'ini', service: 'ini',
}

const BY_FILENAME: Record<string, string> = {
  Dockerfile: 'dockerfile',
  Makefile: 'makefile',
}

export const PATH_LANGUAGES: readonly string[] = [
  ...new Set([...Object.values(BY_EXTENSION), ...Object.values(BY_FILENAME)]),
]

/**
 * Lower-case extensions that name a source or doc file: every highlighted one,
 * plus the plain-text kinds with no highlighter (`txt`, `lock`).
 */
export const KNOWN_FILE_EXTENSIONS: ReadonlySet<string> = new Set([...Object.keys(BY_EXTENSION), 'txt', 'lock'])

/** The highlight.js language for `path`, or null to render it as plain text. */
export function languageForPath(path: string): string | null {
  const name = path.slice(path.lastIndexOf('/') + 1)
  if (BY_FILENAME[name]) return BY_FILENAME[name]
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return null
  return BY_EXTENSION[name.slice(dot + 1).toLowerCase()] ?? null
}
