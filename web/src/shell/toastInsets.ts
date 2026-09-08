import * as React from 'react'

/**
 * Where a toast is allowed to sit, measured from the layout it must not cover.
 *
 * ## The problem this solves
 *
 * Sonner positions its toaster against the VIEWPORT (`position: fixed`), but
 * every piece of chrome it wants to avoid — `TopBar`, `NavRail`, a pushed
 * drawer, the page header, the dock — is an in-flow flex item whose size is
 * never named anywhere (there is no `--header-height`). So the toaster could
 * only guess, and its guess landed the phone strip on the TopBar's Assistant
 * and Help buttons and the desktop card on the page header's action cluster.
 *
 * ## The contract
 *
 * Two kinds of element register themselves here:
 *
 * - **`band`** — the one content band between the rail/drawer columns and
 *   below the TopBar and banners (`AppShell`'s list+main wrapper). Its top,
 *   left and right edges bound the toaster. There is exactly one.
 * - **`clear`** — a header a toast must never cover (`SessionHeader`, the
 *   list column's scope switch, `PageHeader`). The toaster's top edge is
 *   pushed below the LOWEST registered header's bottom. Register only chrome
 *   that lives at the top of the band: a header registered mid-page pushes
 *   every toast to mid-page, by design — it said "never cover me".
 *
 * The result is published as `--toast-inset-top/left/right` on `:root`, in
 * px, and `AppToaster` folds them into sonner's `offset`/`mobileOffset`. CSS
 * variables rather than React state because `AppToaster` is mounted OUTSIDE
 * the app tree (`main.tsx`, outside `ErrorBoundary`): when the shell crashes
 * and unmounts, every anchor unregisters, the variables are removed, and the
 * toaster falls back to the safe-area viewport edge — exactly the surface a
 * crash report needs.
 *
 * Re-measured on every register/unregister (route changes swap headers), on
 * `ResizeObserver` for each anchor (drawer push, rail toggle, keyboard up,
 * composer growth all change the band's box), and on window resize. Reads are
 * coalesced into one animation frame.
 */

export type ToastAnchorRole = 'band' | 'clear'

export const TOAST_INSET_VARS = {
  top: '--toast-inset-top',
  left: '--toast-inset-left',
  right: '--toast-inset-right',
} as const

export interface ToastInsets {
  /** px from the viewport top to where a toast may start. */
  top: number
  /** px from the viewport left edge to the band's left edge. */
  left: number
  /** px from the viewport right edge to the band's right edge. */
  right: number
}

/** Pure: the geometry, so the tests can pin it without a DOM. */
export function computeToastInsets(
  band: DOMRect | null,
  clears: readonly DOMRect[],
  viewportWidth: number,
): ToastInsets | null {
  if (!band) return null
  let top = band.top
  for (const c of clears) {
    // A zero-height rect is an element that is not laid out (`hidden` page,
    // `display: none` head) — it covers nothing, so it clears nothing.
    if (c.height > 0 && c.bottom > top) top = c.bottom
  }
  return {
    top: Math.max(0, top),
    left: Math.max(0, band.left),
    right: Math.max(0, viewportWidth - band.right),
  }
}

const anchors = new Map<Element, ToastAnchorRole>()
let observer: ResizeObserver | null = null
let frame: number | null = null

function publish(): void {
  let band: DOMRect | null = null
  const clears: DOMRect[] = []
  for (const [el, role] of anchors) {
    const r = el.getBoundingClientRect()
    if (role === 'band') band = r
    else clears.push(r)
  }
  const insets = computeToastInsets(band, clears, window.innerWidth)
  const style = document.documentElement.style
  if (!insets) {
    for (const v of Object.values(TOAST_INSET_VARS)) style.removeProperty(v)
    return
  }
  style.setProperty(TOAST_INSET_VARS.top, `${Math.round(insets.top)}px`)
  style.setProperty(TOAST_INSET_VARS.left, `${Math.round(insets.left)}px`)
  style.setProperty(TOAST_INSET_VARS.right, `${Math.round(insets.right)}px`)
}

function schedule(): void {
  if (frame !== null) return
  frame = requestAnimationFrame(() => {
    frame = null
    publish()
  })
}

/** Measure and publish now, discarding any pending frame. Tests; nothing in the app needs it. */
export function flushToastInsets(): void {
  if (frame !== null) {
    cancelAnimationFrame(frame)
    frame = null
  }
  publish()
}

/**
 * Register `el` as a toast anchor. Returns the unregister function.
 * Prefer `useToastAnchor` from a component; this is the primitive under it.
 */
export function registerToastAnchor(el: Element, role: ToastAnchorRole): () => void {
  if (anchors.size === 0) window.addEventListener('resize', schedule)
  anchors.set(el, role)
  observer ??= new ResizeObserver(schedule)
  observer.observe(el)
  schedule()
  return () => {
    anchors.delete(el)
    observer?.unobserve(el)
    if (anchors.size === 0) window.removeEventListener('resize', schedule)
    schedule()
  }
}

/**
 * A callback ref that registers the element for the toaster's lifetime on the
 * page. React 19 calls the cleanup a callback ref returns on unmount, so the
 * register/unregister pair lives in one place.
 */
export function useToastAnchor(role: ToastAnchorRole): (el: HTMLElement | null) => (() => void) | undefined {
  return React.useCallback((el: HTMLElement | null) => (el ? registerToastAnchor(el, role) : undefined), [role])
}
