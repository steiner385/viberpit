// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
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

  /* The legacy plotroom sheet is gone (finish-line row 5, Task 4): every
     non-chart rule became a token utility on its component. Its reappearance
     would be a second stylesheet for one feature. */
  it('the legacy src/pipeline/plotroom.css no longer exists', () => {
    expect(existsSync(join(here, '..', '..', '..', 'pipeline', 'plotroom.css'))).toBe(false)
  })

  /* #1407 (G09) — chart text participates in the kiosk type scale (AC-1.1).
     jsdom/vitest never applies this stylesheet's rules (vitest.config.ts's
     `css.include` allowlists only index.css, deliberately, for suite speed),
     so a render-and-read-computed-style test can't observe the real cascade.
     This asserts the same thing structurally instead: every `font-size` in
     the file resolves through a `--fs-*` step, and every `--fs-*` step index.css
     defines is `calc(<rem> * var(--scale))` — by construction, a font-size
     that reads one of these tokens scales proportionally whenever `--scale`
     changes (AppShell's kiosk `--scale: 1.25`), with no separate assertion
     needed per declaration. */
  describe('#1407 — chart text scales with kiosk --scale, not a literal px', () => {
    const fontSizeDeclarations = [...css.matchAll(/font-size:\s*([^;]+);/g)].map((m) => m[1]!.trim())

    it('has font-size declarations to check (guards the regex above going stale)', () => {
      expect(fontSizeDeclarations.length).toBeGreaterThan(0)
    })

    it('every font-size reads a --fs-* custom property — never a literal px/rem/em', () => {
      for (const value of fontSizeDeclarations) {
        expect(value).toMatch(/^var\(--fs-[a-z0-9]+\)$/)
      }
    })

    it('never reads a step below the --fs-base floor (18px secondary at kiosk --scale, OQ-2)', () => {
      // --fs-2xs/-xs/-sm all land under the 18px kiosk floor (index.css); this file
      // must not reintroduce one of the tokens under --fs-base.
      expect(css).not.toMatch(/font-size:\s*var\(--fs-(2xs|xs|sm)\)/)
    })

    it('reserves --fs-xl for exactly the two single-glance status glyphs', () => {
      expect(css).toMatch(/\.node \.c \{[^}]*font-size:\s*var\(--fs-xl\)/)
      expect(css).toMatch(/\.ops-health \{[^}]*font-size:\s*var\(--fs-xl\)/)
      expect(fontSizeDeclarations.filter((v) => v === 'var(--fs-xl)')).toHaveLength(2)
    })

    it('index.css defines every --fs-* step used here as calc(<rem> * var(--scale))', () => {
      const usedSteps = new Set(fontSizeDeclarations.map((v) => v.match(/--fs-([a-z0-9]+)/)![1]))
      for (const step of usedSteps) {
        expect(indexCss).toMatch(new RegExp(`--fs-${step}:\\s*calc\\([0-9.]+rem \\* var\\(--scale\\)\\)`))
      }
    })
  })

  /* #1407 (G09) — portrait kiosk composition (AC-2.4/2.5): the queue train's
     pointer-only horizontal scroll is replaced by wrapping when PipelineView
     applies `.pipeline-portrait` (driven by `useViewport().portrait` — see
     pipeline/PipelineView.test.tsx for the component-side wiring). No second
     orientation mechanism is introduced by this stylesheet. */
  describe('#1407 — portrait kiosk: queue train wraps instead of scrolling', () => {
    it('wraps .queue-train under .pipeline-portrait, dropping the horizontal scroll', () => {
      expect(css).toMatch(/\.pipeline-portrait \.queue-train \{[^}]*flex-wrap:\s*wrap/)
      expect(css).toMatch(/\.pipeline-portrait \.queue-train \{[^}]*overflow-x:\s*visible/)
    })

    it('introduces no second orientation media query alongside the class hook', () => {
      expect(css).not.toMatch(/@media[^{]*orientation/)
    })
  })
})
