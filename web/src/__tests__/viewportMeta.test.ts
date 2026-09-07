// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

/**
 * Regression guard for the Android soft-keyboard header bug.
 *
 * The app is a flex column sized by `#root { height: 100dvh }` (index.css). On
 * Android Chrome the viewport's
 * default `interactive-widget=resizes-visual` means the soft keyboard overlays the
 * page without shrinking the *layout* viewport, so `dvh` stays full-height and the
 * browser scrolls the top bar off-screen to keep the focused composer visible.
 *
 * `interactive-widget=resizes-content` opts into shrinking the layout viewport (so
 * `dvh` tracks the area above the keyboard) and the column reflows with the header
 * pinned. If this token is ever dropped from index.html, the header silently
 * disappears behind the keyboard on Android again.
 */
// Vitest runs with cwd at the web/ package root, where index.html lives.
const indexHtml = readFileSync(path.resolve(process.cwd(), 'index.html'), 'utf-8')
const indexCss = readFileSync(path.resolve(process.cwd(), 'src/index.css'), 'utf-8')

describe('index.html viewport meta', () => {
  it('opts into interactive-widget=resizes-content so the keyboard shrinks dvh (header stays visible on Android)', () => {
    const viewport = indexHtml.match(/<meta[^>]*name="viewport"[^>]*>/s)?.[0] ?? ''
    expect(viewport).toContain('interactive-widget=resizes-content')
  })

  it('keeps viewport-fit=cover so safe-area insets still clear the gesture bar (#329)', () => {
    const viewport = indexHtml.match(/<meta[^>]*name="viewport"[^>]*>/s)?.[0] ?? ''
    expect(viewport).toContain('viewport-fit=cover')
  })
})

/**
 * The other half of `resizes-content`: opting the keyboard into shrinking the
 * LAYOUT viewport keeps the header on screen, but it also means every region
 * really does get squeezed — on a phone the whole shell reflows into ~480px.
 * Regions that carry optional chrome need a way to drop it at that height, and
 * `short:` is that way. One named variant so the threshold is defined once
 * rather than re-typed as an arbitrary `[@media(max-height:…)]` at each use.
 *
 * It is a CSS variant, not a `useViewport()` field, deliberately: this is a
 * pure show/hide with no JS branch behind it, and the responsive-variant idiom
 * (`hidden sm:block`) is already how this codebase expresses that. `useViewport`
 * stays the only JS reader of a layout query — see its own guard test.
 */
describe('index.css `short` variant', () => {
  it('defines a named max-height variant so regions can shed optional chrome', () => {
    expect(indexCss).toMatch(/@custom-variant\s+short\s*\(\s*@media\s*\(max-height:\s*\d+px\)\s*\)/)
  })

  it('sets the threshold above a keyboard-open phone and below a real window', () => {
    const px = Number(
      indexCss.match(/@custom-variant\s+short\s*\(\s*@media\s*\(max-height:\s*(\d+)px\)\s*\)/)?.[1],
    )
    // A phone with the keyboard up measures ~420–500 CSS px tall; a laptop
    // window with browser chrome is ~600+. Anything outside this band either
    // misses the case it exists for or strips the chrome off a real desktop.
    expect(px).toBeGreaterThanOrEqual(500)
    expect(px).toBeLessThanOrEqual(600)
  })
})
