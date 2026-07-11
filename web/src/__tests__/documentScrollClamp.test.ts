import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

/**
 * Regression guard for the scrolled-document whitespace bug.
 *
 * The app is a fixed-viewport flex column; the *document* must never scroll.
 * Before this clamp, `#root` used `min-height: 100svh` (free to grow) and
 * `html`/`body` had no overflow rule, so any descendant that laid out past the
 * viewport — even transiently, e.g. mid-render before flex constraints settled —
 * made the page itself scrollable. A subsequent focus()/scrollIntoView then
 * scrolled the whole app partly off-screen, leaving dead whitespace below the
 * composer that persisted until reload.
 *
 * Shells must not re-measure the viewport (`h-dvh`/`h-screen`) either; they
 * derive height from the clamped root via `flex-1 min-h-0` (guarded in
 * SessionView.test.tsx).
 */
// Vitest runs with cwd at the web/ package root.
const indexCss = readFileSync(path.resolve(process.cwd(), 'src/index.css'), 'utf-8')

function ruleBody(selectorPattern: RegExp): string {
  const m = indexCss.match(selectorPattern)
  // Strip comments so prose mentioning a property (e.g. "not `min-height`")
  // doesn't trip the declaration assertions.
  return (m?.[1] ?? '').replace(/\/\*[\s\S]*?\*\//g, '')
}

describe('index.css document scroll clamp', () => {
  it('clamps html/body so the document can never scroll', () => {
    const body = ruleBody(/html,\s*body\s*\{([^}]*)\}/s)
    expect(body).toMatch(/overflow:\s*hidden/)
    expect(body).toMatch(/height:\s*100%/)
  })

  it('gives #root a fixed dvh height (min-height would let banners grow the column past the viewport)', () => {
    const body = ruleBody(/#root\s*\{([^}]*)\}/s)
    expect(body).toMatch(/height:\s*100dvh/)
    expect(body).not.toMatch(/min-height/)
  })
})
