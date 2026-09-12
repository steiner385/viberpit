// @vitest-environment node
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * Guard for the 7d final review's Recommendation 2 — a `vi.mock` call whose
 * relative-path spec no longer resolves is a SILENTLY INERT stub: vitest
 * cannot find the module to mock, the factory never runs, and the real
 * implementation executes in its place. That is exactly how the seven
 * relative-path mocks fixed alongside this test went dead (I1) — a file move
 * under the `components/` retirement left the string behind. A stub that
 * resolves is cheap to verify continuously; one that silently stopped
 * resolving is not caught by any test the stub itself guards, because the
 * stub never ran.
 *
 * Paths resolve from THIS FILE via `import.meta.url`, not `process.cwd()` —
 * same rule as `dedupeMobileDesktop.test.ts` in this directory: a guard that
 * silently cannot find its subject is worse than no guard.
 */
const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const SRC_ROOT = path.resolve(WEB_ROOT, 'src')

const CANDIDATE_SUFFIXES = ['', '.ts', '.tsx', '/index.ts', '/index.tsx']

/** Every `*.test.ts(x)` file under `web/src`, discovered rather than listed. */
function testFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules') continue
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) {
      out.push(...testFiles(full))
    } else if (/\.test\.tsx?$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

/** `{ line, spec }` for every `vi.mock('<spec>'` / `vi.doMock('<spec>'` string
 *  literal call in `text` (single or double quoted). */
function mockSpecs(text: string): { line: number; spec: string }[] {
  const out: { line: number; spec: string }[] = []
  const re = /\bvi\.(?:do)?[Mm]ock\(\s*(['"])((?:(?!\1).)*)\1/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const line = text.slice(0, m.index).split('\n').length
    out.push({ line, spec: m[2] })
  }
  return out
}

/** Does `spec` resolve to a real file, per the candidate-suffix list? */
function resolves(baseAbsPath: string): boolean {
  return CANDIDATE_SUFFIXES.some((suffix) => existsSync(baseAbsPath + suffix))
}

describe('vi.mock specs resolve to a real file', () => {
  it('every relative or @/-aliased vi.mock/vi.doMock spec under web/src resolves', () => {
    const unresolved: string[] = []
    for (const file of testFiles(SRC_ROOT)) {
      const text = readFileSync(file, 'utf-8')
      for (const { line, spec } of mockSpecs(text)) {
        let baseAbsPath: string | null = null
        if (spec.startsWith('./') || spec.startsWith('../')) {
          baseAbsPath = path.resolve(path.dirname(file), spec)
        } else if (spec.startsWith('@/')) {
          baseAbsPath = path.resolve(SRC_ROOT, spec.slice(2))
        }
        // Bare package specs (e.g. 'heic2any') are skipped — node_modules
        // resolution is not this guard's concern.
        if (baseAbsPath === null) continue
        if (!resolves(baseAbsPath)) {
          unresolved.push(`${path.relative(WEB_ROOT, file)}:${line} → ${spec}`)
        }
      }
    }
    expect(unresolved, `unresolvable vi.mock/vi.doMock specs:\n${unresolved.join('\n')}`).toEqual([])
  })
})
