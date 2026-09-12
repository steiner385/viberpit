// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * Regression guard (R7e-9, carried into 7f) — a class-string ternary whose two
 * legs are textually IDENTICAL is dead code and a lost visual cue, and it is
 * exactly the failure mode the row 7 token fixer introduces silently: the
 * fixer maps two distinct source shades (e.g. `text-gray-400` and
 * `text-gray-600`) onto ONE token (`text-ink-muted`), so a pre-fixer
 * `cond ? 'text-gray-400' : 'text-gray-600'` — a real light/dark or
 * state-dependent cue — becomes a ternary with the SAME `text-ink-muted`
 * token on both legs post-fix (dead code, one lost cue). That happened three
 * times across the row 7 sweep before a human caught it in review; this test
 * makes the class of bug self-catching instead of relying on the next
 * reviewer to spot it by eye. (This paragraph deliberately never writes the
 * collapsed form out literally — with both legs quoted and identical — or it
 * would trip the very check it describes.)
 *
 * Scoped to string/template ternaries generally (not just className-attribute
 * ones) because a collapsed ternary is dead code regardless of context, and a
 * narrower class-context-only check would need the same AST plumbing as
 * `eslint-rules/no-raw-palette.js` for no extra safety — a plain source walk
 * with three backreferencing regexes (JS `RegExp` supports `\1`; no `grep -P`
 * needed) catches every quote style.
 *
 * Every `.ts`/`.tsx` file under `web/src` is walked, `__tests__` included
 * (deliberately NOT skipped — the same bug can hide in a test's own fixture
 * string), `node_modules`/`dist` skipped. Paths resolve from THIS FILE via
 * `import.meta.url`, not `process.cwd()` — same rule as `mockPathsResolve.test.ts`
 * and `dedupeMobileDesktop.test.ts` in this directory: a guard that silently
 * cannot find its subject is worse than no guard.
 */
const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const SRC_ROOT = path.resolve(WEB_ROOT, 'src')

const SKIP_DIRS = new Set(['node_modules', 'dist'])

/** Every `.ts`/`.tsx` file under `dir`, discovered rather than listed. */
function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
    } else if (/\.tsx?$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

// One pattern per quote style; a backreference (`\1`) requires the closing
// leg to be byte-for-byte identical to the opening one — not just similar.
// The separators are `\s*`, not ` *`: prettier wraps a long class ternary onto
// three lines (`cond`, `? '…'`, `: '…'`), and a literal-space class cannot
// cross that newline — the collapsed shape would slip through unseen. `\s*`
// also covers tab indentation. Verified against all of `web/src`: 0 hits, so
// the wider form costs no false positives.
const COLLAPSED_TERNARY_PATTERNS = [
  /\?\s*'([^']+)'\s*:\s*'\1'/g,
  /\?\s*"([^"]+)"\s*:\s*"\1"/g,
  /\?\s*`([^`]+)`\s*:\s*`\1`/g,
]

/** `{ line, leg }` for every collapsed ternary found in `text`. */
function collapsedTernaries(text: string): { line: number; leg: string }[] {
  const out: { line: number; leg: string }[] = []
  for (const re of COLLAPSED_TERNARY_PATTERNS) {
    let m: RegExpExecArray | null
    re.lastIndex = 0
    while ((m = re.exec(text))) {
      const line = text.slice(0, m.index).split('\n').length
      out.push({ line, leg: m[1] })
    }
  }
  return out
}

describe('no collapsed class ternaries', () => {
  it('no `cond ? X : X` string/template ternary exists under web/src', () => {
    const hits: string[] = []
    for (const file of sourceFiles(SRC_ROOT)) {
      const text = readFileSync(file, 'utf-8')
      for (const { line, leg } of collapsedTernaries(text)) {
        hits.push(`${path.relative(WEB_ROOT, file)}:${line} → both legs are '${leg}'`)
      }
    }
    expect(hits, `collapsed (dead) class ternaries:\n${hits.join('\n')}`).toEqual([])
  })
})
