import { useLayoutEffect, useState, type RefCallback } from 'react'

/** Root font size in px — what `rem` resolves against in a container query. */
function rootFontPx(): number {
  return parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
}

/**
 * A container query in JS: whether the element's OWN width is at least
 * `minRem` (`@container (min-width: <minRem>rem)`), for a layout whose switch
 * also changes React state — the session rail moves its cards into a Details
 * tab, and `?view=details` must resolve against that. Keyed on the element,
 * not the viewport: the assistant drawer narrows the session pane without
 * resizing the window.
 *
 * Measured before paint and on every resize. Returns a callback ref to attach
 * and the match; false until the element mounts.
 */
export function useContainerMinWidth<T extends HTMLElement>(minRem: number): [RefCallback<T>, boolean] {
  const [el, setEl] = useState<T | null>(null)
  const [matches, setMatches] = useState(false)

  useLayoutEffect(() => {
    if (!el) return
    const measure = () => setMatches(el.offsetWidth >= minRem * rootFontPx())
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [el, minRem])

  return [setEl, matches]
}
