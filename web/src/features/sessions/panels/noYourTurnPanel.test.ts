// @vitest-environment node
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * D-11: the post-turn summary card (`transcript/TurnSummaryCard`) replaced the
 * pinned `YourTurnPanel`, its "Likely complete" branch included. Nothing may
 * import the panel again — tests included, since a test that renders it pins a
 * surface that no longer ships. Paths resolve from THIS FILE via `import.meta.url`.
 */
const SELF = fileURLToPath(import.meta.url)
const SRC_ROOT = path.resolve(path.dirname(SELF), '../../..')
const SKIP_DIRS = new Set(['node_modules', 'dist'])
const IMPORT = /from\s+['"][^'"]*\/YourTurnPanel['"]/

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full))
    else if (/\.tsx?$/.test(name)) out.push(full)
  }
  return out
}

describe('YourTurnPanel is gone', () => {
  it('the guard can fail: it matches an import of the panel', () => {
    expect(IMPORT.test(`import { YourTurnPanel } from './YourTurnPanel'`)).toBe(true)
    expect(IMPORT.test(`import { YourTurnPanel } from '@/features/sessions/panels/YourTurnPanel'`)).toBe(true)
  })

  it('no source or test file imports YourTurnPanel, and the file is deleted', () => {
    // This file names the pattern in its own self-check.
    const offenders = sourceFiles(SRC_ROOT)
      .filter((f) => f !== SELF)
      .filter((f) => IMPORT.test(readFileSync(f, 'utf-8')))
      .map((f) => path.relative(SRC_ROOT, f))
    expect(offenders).toEqual([])
    expect(existsSync(path.join(SRC_ROOT, 'features/sessions/panels/YourTurnPanel.tsx'))).toBe(false)
  })
})
