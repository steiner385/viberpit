/**
 * Shared braille spinner — the classic Claude-Code-style cycling dot glyph.
 *
 * Single source of truth for the animated spinner used by both WorkingIndicator
 * ("waiting for Claude") and SessionPlaceholder ("connecting / loading"). Keeping
 * the frames + reduced-motion handling here avoids a second, drifting copy.
 */
import { useState, useEffect } from 'react'

/** Cycling braille spinner frames. */
export const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] as const
/** Static glyph shown instead of the cycling spinner under reduced-motion. */
export const STATIC_SPINNER = '⠿'
const SPINNER_INTERVAL_MS = 80

/** Returns true when the user has requested reduced motion. Safe in jsdom (matchMedia may be absent). */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * Returns the current braille spinner glyph: cycling at 80ms while motion is
 * allowed, or a single static glyph under prefers-reduced-motion. Drive a
 * `<span data-testid="…">` with the returned value.
 */
export function useBrailleSpinner(): string {
  const reducedMotion = prefersReducedMotion()
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    if (reducedMotion) return
    const id = setInterval(() => {
      setFrame((f) => (f + 1) % SPINNER_FRAMES.length)
    }, SPINNER_INTERVAL_MS)
    return () => clearInterval(id)
  }, [reducedMotion])
  return reducedMotion ? STATIC_SPINNER : SPINNER_FRAMES[frame]
}
