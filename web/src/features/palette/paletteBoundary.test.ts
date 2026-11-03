// @vitest-environment node
/**
 * The palette is a LAZY chunk (D-10): the only runtime path into
 * `features/palette` is `shell/regions/Overlays`' `import()`. One static
 * `import { … } from '@/features/palette/…'` anywhere outside the folder pulls
 * the palette, its search and the settings metadata back into the entry chunk —
 * silently, until `npm run budget` notices kilobytes it cannot attribute. This
 * pins the boundary at the source, where the mistake is made.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

const SRC = join(__dirname, '..', '..')
const PALETTE = join(SRC, 'features', 'palette')

function sources(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...sources(p))
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p)
  }
  return out
}

/** Every `import … from '<spec>'` / `export … from '<spec>'` statement, whole. */
function staticImports(src: string): { stmt: string; spec: string }[] {
  return [...src.matchAll(/^(?:import|export)\s[^;]*?from\s+'([^']+)'/gms)].map((m) => ({ stmt: m[0], spec: m[1] }))
}

const intoPalette = (file: string, spec: string): boolean =>
  spec.startsWith('@/features/palette')
  || (spec.startsWith('.') && join(file, '..', spec).startsWith(PALETTE + sep))

describe('the palette lazy boundary', () => {
  it('nothing outside features/palette imports it at runtime — type-only imports are erased', () => {
    const offenders: string[] = []
    for (const file of sources(SRC)) {
      if (file.startsWith(PALETTE + sep)) continue
      for (const { stmt, spec } of staticImports(readFileSync(file, 'utf8'))) {
        if (intoPalette(file, spec) && !/^(?:import|export)\s+type\s/.test(stmt)) {
          offenders.push(`${relative(SRC, file)}: ${stmt.replace(/\s+/g, ' ')}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('Overlays reaches it through `import()` — the lazy chunk’s one door', () => {
    const overlays = readFileSync(join(SRC, 'shell', 'regions', 'Overlays.tsx'), 'utf8')
    expect(overlays).toMatch(/React\.lazy\(\(\) => import\('@\/features\/palette'\)/)
  })
})
