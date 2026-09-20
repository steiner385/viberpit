// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * `docs/PAGE-TABS.md` §7: `web/src/ui/Tabs.tsx` is the ONLY component that
 * renders `role="tablist"`. A second tablist is a parallel implementation
 * (constitution VI) that drifts from the primitive's keyboard model and
 * badge semantics — the session body carried one for months before the
 * standard caught it. Tests are excluded: a test may assert the role by
 * name. Paths resolve from THIS FILE via `import.meta.url`, the rule shared
 * by every guard in this directory.
 */
const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const SRC_ROOT = path.resolve(WEB_ROOT, 'src')
const ALLOWED = path.join(SRC_ROOT, 'ui', 'Tabs.tsx')
const SKIP_DIRS = new Set(['node_modules', 'dist'])

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full))
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(full)
  }
  return out
}

// JSX attribute, object-literal prop, and `role: 'tablist' as const` shapes.
const TABLIST = /role\s*[:=]\s*["'`]tablist["'`]/

export function tablistOffenders(files: string[] = sourceFiles(SRC_ROOT)): string[] {
  const hits: string[] = []
  for (const file of files) {
    if (file === ALLOWED) continue
    const text = readFileSync(file, 'utf-8')
    const lines = text.split('\n')
    lines.forEach((line, i) => {
      if (TABLIST.test(line)) hits.push(`${path.relative(WEB_ROOT, file)}:${i + 1}`)
    })
  }
  return hits
}

describe('one tablist primitive', () => {
  it('no role="tablist" outside ui/Tabs.tsx', () => {
    const hits = tablistOffenders()
    expect(hits, `render tabs through ui/Tabs (docs/PAGE-TABS.md §7):\n${hits.join('\n')}`).toEqual([])
  })

  it('the scan sees a tablist when one exists', () => {
    // The primitive itself is the fixture: it must match, or the guard is blind.
    expect(readFileSync(ALLOWED, 'utf-8')).toMatch(TABLIST)
  })
})
