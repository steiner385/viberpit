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
import { languageForPath } from './pathLanguages'

const LANGUAGES = {
  bash, css, dockerfile, go, ini, javascript, json, makefile, markdown, python, rust, scss, sql, typescript, xml, yaml,
}
for (const [name, lang] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, lang)

export function registeredLanguages(): string[] {
  return hljs.listLanguages()
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
