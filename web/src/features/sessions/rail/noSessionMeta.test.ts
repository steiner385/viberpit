// @vitest-environment node
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * D-6: the session rail (`SessionRail` + `SessionToolbar`) replaced the
 * `SessionMeta` status strip. Nothing may import it again — tests included,
 * since a test that mocks or renders the strip pins a surface that no longer
 * ships. Paths resolve from THIS FILE via `import.meta.url`.
 */
const SELF = fileURLToPath(import.meta.url)
const SRC_ROOT = path.resolve(path.dirname(SELF), '../../..')
const SKIP_DIRS = new Set(['node_modules', 'dist'])
const IMPORT = /from\s+['"][^'"]*\/SessionMeta['"]/

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

describe('SessionMeta is gone', () => {
  it('the guard can fail: it matches an import of the strip', () => {
    expect(IMPORT.test(`import { SessionMeta } from './SessionMeta'`)).toBe(true)
    expect(IMPORT.test(`import { SessionMeta } from '@/features/sessions/SessionMeta'`)).toBe(true)
  })

  it('no source or test file imports SessionMeta, and the file is deleted', () => {
    // This file names the pattern in its own self-check.
    const offenders = sourceFiles(SRC_ROOT)
      .filter((f) => f !== SELF)
      .filter((f) => IMPORT.test(readFileSync(f, 'utf-8')))
      .map((f) => path.relative(SRC_ROOT, f))
    expect(offenders).toEqual([])
    expect(existsSync(path.join(SRC_ROOT, 'features/sessions/SessionMeta.tsx'))).toBe(false)
  })
})
