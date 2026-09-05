// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const css = readFileSync(join(__dirname, '..', 'plotroom.css'), 'utf8')
const root = readFileSync(join(__dirname, '..', '..', 'index.css'), 'utf8')

// The first `.prdash-root { … }` block is the token block.
const block = css.slice(css.indexOf('.prdash-root {'), css.indexOf('}', css.indexOf('.prdash-root {')) + 1)

describe('plotroom token remap', () => {
  it('defines no color literals in the .prdash-root token block', () => {
    expect(block).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
  })
  it('remaps every legacy token onto a global one', () => {
    for (const v of ['--bg', '--surface', '--text', '--muted', '--border', '--accent', '--done', '--fail', '--purple', '--amber',
      '--accent-strong', '--done-strong', '--amber-strong',
      '--z-raised', '--z-sticky-header', '--z-bottom-nav', '--z-popover', '--z-overlay', '--z-drawer', '--z-command-palette']) {
      expect(block, v).toMatch(new RegExp(v.replace(/-/g, '\\-') + ':\\s*var\\(--(t|layer)-'))
    }
  })
  it('has no dark-scheme override for .prdash-root (the global tokens flip instead)', () => {
    expect(css).not.toMatch(/prefers-color-scheme:\s*dark\)\s*\{\s*\.prdash-root/)
  })
  it('keeps the legacy aliases on :root for projects.css / initiatives.css', () => {
    for (const v of ['--bg', '--surface', '--text', '--muted', '--border', '--accent', '--amber', '--done', '--fail', '--purple']) {
      expect(root).toMatch(new RegExp(v.replace(/-/g, '\\-') + ':\\s*var\\(--t-'))
    }
  })
})
