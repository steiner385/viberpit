// The diff highlighter: highlight.js core plus only the languages the path map
// below can name. Reached only through `highlightChunk.ts`, which loads it lazily —
// `DiffRenderer` is on the transcript's first paint, and the bundle budget has
// no room for highlight.js there. Import this file statically and it lands back
// in the initial payload; `npm run budget` is what says so.
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import go from 'highlight.js/lib/languages/go'
import ini from 'highlight.js/lib/languages/ini'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import makefile from 'highlight.js/lib/languages/makefile'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import rust from 'highlight.js/lib/languages/rust'
import scss from 'highlight.js/lib/languages/scss'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
// The same theme the markdown renderer uses — one highlight theme app-wide.
import 'highlight.js/styles/github-dark.css'

const LANGUAGES = {
  bash, css, dockerfile, go, ini, javascript, json, makefile, markdown, python, rust, scss, sql, typescript, xml, yaml,
}
for (const [name, lang] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, lang)

export function registeredLanguages(): string[] {
  return hljs.listLanguages()
}

// File path → language. It lives in this chunk, not beside the renderer, to keep
// it off the first-paint bundle; a file with no known language costs one cached
// chunk load. Every value must be registered above (a test holds the two together).
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

/** The highlight.js language for `path`, or null to render it as plain text. */
export function languageForPath(path: string): string | null {
  const name = path.slice(path.lastIndexOf('/') + 1)
  if (BY_FILENAME[name]) return BY_FILENAME[name]
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return null
  return BY_EXTENSION[name.slice(dot + 1).toLowerCase()] ?? null
}

/** A line highlighter for `path`, or null to render it as plain text. */
export function highlighterFor(path: string): ((text: string) => string) | null {
  const language = languageForPath(path)
  return language ? (text) => highlightLine(text, language) : null
}

/**
 * One source line as highlight.js markup. highlight.js HTML-escapes the source
 * text itself; the only markup in the result is its own `<span class="hljs-…">`
 * tokens, which is what makes it safe to inject. Lines are highlighted one at a
 * time — a diff shows fragments, so a construct spanning lines (a block comment,
 * a template string) may lose its colour past the first line.
 */
export function highlightLine(text: string, language: string): string {
  return hljs.highlight(text, { language, ignoreIllegals: true }).value
}
