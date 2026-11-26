// @vitest-environment node
import { describe, it, expect } from 'vitest'
// Raw source, not the compiled sheet: we need the three authored theme blocks
// (`:root`, the prefers-color-scheme override, `:root[data-theme="dark"]`) with
// their literal `--t-*: #rrggbb` values. tokenBridge.test.ts imports the same
// file with `?inline` for the *compiled* utilities; this is the other half.
import css from '@/index.css?raw'

/*
 * Colour-contrast conformance for the design tokens (WCAG 2.x, AA).
 *
 * Why this file exists as pure arithmetic and not as an axe assertion: the axe
 * checks in the component tests run in jsdom, which applies no stylesheet, so
 * axe's `color-contrast` rule always reports `incomplete` — and
 * `toHaveNoViolations()` only inspects `violations`. Those assertions therefore
 * cannot fail on contrast, and did not when four token pairs were measurably
 * below 4.5:1. This test reads the tokens straight out of index.css and does
 * the maths, so a regression in a hex value is caught at the source.
 *
 * All pairs here render normal-size text (12px `font-medium` pills/banners), so
 * the AA threshold is 4.5:1 — not the 3:1 large-text allowance.
 */

const AA_NORMAL_TEXT = 4.5

type Tokens = Record<string, string>

/** Slice out the `{ … }` body that follows `selector`, honouring nesting. */
function blockBody(source: string, selector: string): string {
  const at = source.indexOf(selector)
  if (at === -1) throw new Error(`index.css: no block found for selector \`${selector}\``)
  let i = source.indexOf('{', at)
  if (i === -1) throw new Error(`index.css: selector \`${selector}\` has no body`)
  let depth = 0
  const start = i + 1
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i)
  }
  throw new Error(`index.css: unterminated block for selector \`${selector}\``)
}

/** Every `--t-name: #rrggbb` declaration in a block, keyed without the `--t-` prefix. */
function parseTokens(body: string): Tokens {
  const out: Tokens = {}
  for (const m of body.matchAll(/--t-([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\b/g)) {
    out[m[1]] = m[2].toLowerCase()
  }
  return out
}

const LIGHT_SELECTOR = ':root {'
const DARK_MEDIA_SELECTOR = ':root:not([data-theme="light"]) {'
const DARK_ATTR_SELECTOR = ':root[data-theme="dark"] {'

const light = parseTokens(blockBody(css, LIGHT_SELECTOR))
const darkMedia = parseTokens(blockBody(css, DARK_MEDIA_SELECTOR))
const darkAttr = parseTokens(blockBody(css, DARK_ATTR_SELECTOR))

// The dark blocks omit `-strong` on purpose (it inherits from `:root` and is
// deliberately theme-invariant), so fall back to the light block for anything
// a dark block does not restate — that is what the cascade actually does.
const resolve = (base: Tokens, overrides: Tokens): Tokens => ({ ...base, ...overrides })

const BLOCKS: Array<{ name: string; tokens: Tokens }> = [
  { name: 'light', tokens: light },
  { name: 'dark (prefers-color-scheme)', tokens: resolve(light, darkMedia) },
  { name: 'dark ([data-theme="dark"])', tokens: resolve(light, darkAttr) },
]

/* ── WCAG 2.x relative luminance + contrast ratio ─────────────────────────── */

function channel(v: number): number {
  const c = v / 255
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}

function luminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16)
  const r = channel((n >> 16) & 0xff)
  const g = channel((n >> 8) & 0xff)
  const b = channel(n & 0xff)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export function contrastRatio(fg: string, bg: string): number {
  const a = luminance(fg)
  const b = luminance(bg)
  const [hi, lo] = a > b ? [a, b] : [b, a]
  return (hi + 0.05) / (lo + 0.05)
}

/** Composite `fg` at `alpha` over `bg` — what `opacity-N` actually paints. */
export function composite(fg: string, bg: string, alpha: number): string {
  const f = parseInt(fg.slice(1), 16)
  const b = parseInt(bg.slice(1), 16)
  const mix = (shift: number) =>
    Math.round(((f >> shift) & 0xff) * alpha + ((b >> shift) & 0xff) * (1 - alpha))
  return '#' + [16, 8, 0].map((s) => mix(s).toString(16).padStart(2, '0')).join('')
}

/* ── The pairs the UI actually renders ────────────────────────────────────── */

/** Tone name → the token supplying its *text* colour. Neutral pills take their
 *  ink from `--t-ink-muted`, not from `--t-neutral`. */
const TONE_TEXT: Record<string, string> = {
  danger: 'danger',
  success: 'success',
  accent: 'accent',
  neutral: 'ink-muted',
  info: 'info',
  warning: 'warning',
}
const TONES = Object.keys(TONE_TEXT)

type Pair = { fg: string; bg: string }

const PAIRS: Pair[] = [
  // Pill / badge: each tone's text on its own wash.
  ...TONES.map((tone) => ({ fg: TONE_TEXT[tone], bg: `${tone}-wash` })),
  // Banner body copy is always `text-ink-muted`, on any of the six washes.
  ...TONES.map((tone) => ({ fg: 'ink-muted', bg: `${tone}-wash` })),
  // Plain body text on a surface.
  { fg: 'ink', bg: 'surface' },
  { fg: 'ink-muted', bg: 'surface' },
]

describe('design-token colour contrast (WCAG 2.x AA, normal text)', () => {
  it('parses all three theme blocks out of index.css', () => {
    expect(Object.keys(light).length).toBeGreaterThan(10)
    expect(Object.keys(darkMedia).length).toBeGreaterThan(10)
    expect(Object.keys(darkAttr).length).toBeGreaterThan(10)
  })

  for (const { name, tokens } of BLOCKS) {
    it(`${name}: every text/background pair clears ${AA_NORMAL_TEXT}:1`, () => {
      const failures: string[] = []
      for (const { fg, bg } of PAIRS) {
        const fgHex = tokens[fg]
        const bgHex = tokens[bg]
        expect(fgHex, `${name}: index.css defines no --t-${fg}`).toBeTruthy()
        expect(bgHex, `${name}: index.css defines no --t-${bg}`).toBeTruthy()
        const ratio = contrastRatio(fgHex, bgHex)
        if (ratio < AA_NORMAL_TEXT) {
          failures.push(
            `${name}: text-${fg} ${fgHex} on bg-${bg} ${bgHex} = ${ratio.toFixed(2)},` +
              ` need >= ${AA_NORMAL_TEXT}`,
          )
        }
      }
      expect(failures, failures.join('\n')).toEqual([])
    })
  }

  it('the two dark blocks declare identical values (a fix must land in both)', () => {
    const drift: string[] = []
    for (const key of new Set([...Object.keys(darkMedia), ...Object.keys(darkAttr)])) {
      if (darkMedia[key] !== darkAttr[key]) {
        drift.push(
          `--t-${key}: prefers-color-scheme block has ${darkMedia[key] ?? '(absent)'},` +
            ` [data-theme="dark"] block has ${darkAttr[key] ?? '(absent)'}`,
        )
      }
    }
    expect(drift, drift.join('\n')).toEqual([])
  })

  // #1402: completed dependencies used `opacity-60` on top of muted ink,
  // which composites well below 4.5:1 and bypasses every token-pair check.
  // Strike-through now carries the "done" meaning on `text-ink-muted` instead.
  for (const { name, tokens } of BLOCKS) {
    it(`${name}: a completed dependency (line-through text-ink-muted) clears ${AA_NORMAL_TEXT}:1`, () => {
      expect(contrastRatio(tokens['ink-muted'], tokens['surface'])).toBeGreaterThanOrEqual(AA_NORMAL_TEXT)
    })
    it(`${name}: the retired opacity-60 treatment is measurably below ${AA_NORMAL_TEXT}:1`, () => {
      const faded = composite(tokens['ink-muted'], tokens['surface'], 0.6)
      expect(contrastRatio(faded, tokens['surface'])).toBeLessThan(AA_NORMAL_TEXT)
    })
  }
})
