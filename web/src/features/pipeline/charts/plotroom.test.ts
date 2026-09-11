// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const here = join(__dirname)
const css = readFileSync(join(here, 'plotroom.css'), 'utf8')

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
})
