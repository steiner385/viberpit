import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

// A fire-and-forget `void x.mutateAsync(...)` leaves the rejection unhandled, so
// `installGlobalErrorHandlers` logs the failure a second time (always at `error`)
// after `handleMutationError` already logged and classified it (#2727). Use
// `mutate(vars, { onSuccess })` instead, or `await` inside a try/catch.
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sources(path)
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : []
  })
}

describe('floating mutateAsync guard', () => {
  it('has no `void ….mutateAsync(` call in non-test source', () => {
    const offenders = sources(join(__dirname, '..')).filter((f) =>
      /\bvoid\s+[\w.]+\.mutateAsync\(/.test(readFileSync(f, 'utf8')),
    )
    expect(offenders).toEqual([])
  })
})
