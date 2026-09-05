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
