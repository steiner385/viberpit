// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const here = join(__dirname)
const css = readFileSync(join(here, 'plotroom.css'), 'utf8')
const indexCss = readFileSync(join(here, '..', '..', '..', 'index.css'), 'utf8')

function cssFilesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? cssFilesUnder(p) : n.endsWith('.css') ? [p] : []
  })
}

describe('charts/plotroom.css — the one allowlisted stylesheet', () => {
  it('is scoped by .pipeline-chart, never by the deleted .prdash-root', () => {
    expect(css).toMatch(/^\.pipeline-chart\s*\{/m)
    expect(css).not.toMatch(/prdash-root/)
  })
  it('has no dark override and no colour literals — tokens only', () => {
    expect(css).not.toMatch(/prefers-color-scheme/)
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(css).not.toMatch(/rgba?\(/)
    expect(css).not.toMatch(/var\(--(bg|surface|text|muted|border|accent|done|fail|amber|purple)\b/)
  })
  it('is the only css file under features/pipeline', () => {
    expect(cssFilesUnder(join(here, '..'))).toEqual([join(here, 'plotroom.css')])
  })

  /* The `-strong` fills are theme-INVARIANT by contract (index.css), so their
     glyph ink must be too: `--t-canvas` follows the theme and would sink
     white-on-success-strong to 3.58:1 and white-on-warning-strong to 3.37:1 in
     dark, under the 4.5:1 AA floor for an 11px glyph. */
  it('inks the theme-invariant `-strong` node fills with --t-on-strong', () => {
    expect(css).toMatch(/\.node\.done \.c \{[^}]*var\(--t-success-strong\)[^}]*var\(--t-on-strong\)/)
    expect(css).toMatch(/\.node\.parked \.c \{[^}]*var\(--t-warning-strong\)[^}]*var\(--t-on-strong\)/)
  })

  it('index.css declares --t-on-strong exactly once — no dark override', () => {
    expect(indexCss.match(/--t-on-strong\s*:/g)).toHaveLength(1)
  })

  /* Inherited from the deleted plotroomTokens.test.ts: src/pipeline/plotroom.css
     is still written against plotroom's own alias names and only resolves
     because index.css keeps them on `:root`. Task 4 deletes that sheet; until
     then, dropping an alias silently unstyles the legacy dashboard. */
  it('index.css still carries the legacy aliases the legacy sheet needs', () => {
    for (const v of ['--bg', '--surface', '--text', '--muted', '--border',
      '--accent', '--amber', '--done', '--fail', '--purple']) {
      expect(indexCss, v).toMatch(new RegExp(v.replace(/-/g, '\\-') + ':\\s*var\\(--t-'))
    }
  })
})
