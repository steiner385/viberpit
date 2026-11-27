/**
 * Assertions for the user-adaptation half of WCAG 2.2 AA (#1416, G18): what happens
 * to the page when the READER changes the conditions — zoom, text size, text
 * spacing, reduced motion, forced colours, keyboard-only focus.
 *
 * axe cannot see any of this: it scans one static render at one size. These
 * measure the engine's layout and computed style after the condition is applied,
 * the same way `geometry.ts` does for the viewport matrix, and the seeded-defect
 * tests in `wcag-adaptations.spec.ts` prove each one can fail.
 *
 * What these CANNOT stand in for is said once, in
 * `docs/audits/wcag-2.2-aa-conformance.md`: an emulated media feature is not a
 * screen reader, a software keyboard or a person.
 */
import { expect, type Page } from '@playwright/test'

/** SC 1.4.10 Reflow: 1280×1024 CSS px at 400% zoom. */
export const REFLOW_VIEWPORT = { width: 320, height: 256 } as const
/** SC 1.4.4 Resize Text by browser zoom: 1280×800 at 200%. */
export const ZOOM_200_VIEWPORT = { width: 640, height: 400 } as const
/** One touch-target row (`--target`): the least a scroll pane must show to be usable. */
export const MIN_SCROLL_PANE_PX = 44

/** SC 1.4.12 Text Spacing — the four overrides, verbatim from the criterion. */
export const TEXT_SPACING_CSS = `
  * { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; }
  p { margin-bottom: 2em !important; }
`
/** Text-only 200% (Firefox "Zoom text only", an OS font-size setting): every size is rem. */
export const TEXT_200_CSS = 'html { font-size: 200% !important; }'
/** Computed style is read mid-transition otherwise; the assertion wants the end state. */
export const NO_TRANSITIONS_CSS = '*, *::before, *::after { transition: none !important; }'

/**
 * Text or controls laid out beyond the left/right edge of the viewport that no
 * horizontally scrolling ancestor can bring back — content the reader cannot reach
 * by scrolling in one direction. (`expectNoHorizontalOverflow` cannot see this in
 * this app: `html, body { overflow: hidden }` means the document never scrolls.)
 */
export async function contentOutsideViewport(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth
    const scrollsSideways = (from: Element | null): boolean => {
      for (let el = from; el; el = el.parentElement) {
        const overflowX = getComputedStyle(el).overflowX
        if ((overflowX === 'auto' || overflowX === 'scroll') && el.scrollWidth > el.clientWidth) return true
      }
      return false
    }
    const offenders: string[] = []
    for (const el of document.querySelectorAll('body *')) {
      if (!(el instanceof HTMLElement)) continue
      const style = getComputedStyle(el)
      if (style.visibility === 'hidden' || style.display === 'none') continue
      const box = el.getBoundingClientRect()
      if (box.width <= 1 || box.height <= 1) continue // nothing, or visually-hidden text
      if (box.right <= viewportWidth + 1 && box.left >= -1) continue
      const carriesContent = el.matches('a, button, input, select, textarea, [role]')
        || [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent?.trim())
      if (!carriesContent || scrollsSideways(el.parentElement)) continue
      const name = (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 40)
      offenders.push(`${el.tagName.toLowerCase()} "${name}" spans ${Math.round(box.left)}..${Math.round(box.right)} of ${viewportWidth}`)
    }
    return offenders
  })
}

export async function expectContentWithinViewport(page: Page): Promise<void> {
  const offenders = await contentOutsideViewport(page)
  expect(offenders, 'content beyond the viewport edge with no way to scroll to it').toEqual([])
}

/**
 * Vertically scrolling panes too short to show one row. The shell is a fixed-height
 * column, so when fixed chrome outgrows a small viewport the document does not
 * scroll — the pane that holds the content is squeezed instead, to nothing.
 */
export async function starvedScrollPanes(page: Page, minPx = MIN_SCROLL_PANE_PX): Promise<string[]> {
  return page.evaluate((min) => {
    const offenders: string[] = []
    for (const el of document.querySelectorAll('body *')) {
      const overflowY = getComputedStyle(el).overflowY
      if (overflowY !== 'auto' && overflowY !== 'scroll') continue
      if (el.scrollHeight <= el.clientHeight + 1 || el.clientHeight >= min) continue
      const region = el.closest('[data-region]')?.getAttribute('data-region') ?? 'no region'
      offenders.push(`${region}: ${el.clientHeight}px visible of ${el.scrollHeight}px`)
    }
    return offenders
  }, minPx)
}

export async function expectScrollPanesUsable(page: Page): Promise<void> {
  const offenders = await starvedScrollPanes(page)
  expect(offenders, `scroll pane shorter than ${MIN_SCROLL_PANE_PX}px — its content is unreachable`).toEqual([])
}

/** Names of animations still running (SC 2.3.3 / 2.2.2 under `prefers-reduced-motion`). */
export async function runningAnimations(page: Page): Promise<string[]> {
  return page.evaluate(() => document.getAnimations()
    .filter((a) => a.playState === 'running')
    .map((a) => (a as CSSAnimation).animationName ?? 'transition'))
}

export interface FocusStop {
  /** Tag plus accessible-ish name, for the failure message. */
  what: string
  /** Something painted differently because of focus (SC 2.4.7). */
  hasIndicator: boolean
  /** A real outline — the only indicator forced colours keeps. */
  hasOutline: boolean
  /** No point of the control is on top at its own position (SC 2.4.11). */
  obscured: boolean
}

/**
 * Tab through the page from the top and describe every stop. Ends when focus
 * returns to a control already seen (the trap or the wrap) or after `max` stops.
 */
export async function walkFocus(page: Page, max = 80): Promise<FocusStop[]> {
  await page.addStyleTag({ content: NO_TRANSITIONS_CSS })
  const stops: FocusStop[] = []
  const seen = new Set<string>()
  for (let i = 0; i < max; i++) {
    await page.keyboard.press('Tab')
    const stop = await page.evaluate(() => {
      const el = document.activeElement
      if (!(el instanceof HTMLElement) || el === document.body) return null
      const paint = () => {
        const s = getComputedStyle(el)
        const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0
        // A stretched-link control draws its ring on `::after` (`ui/StatTiles`).
        const pseudo = ['::before', '::after'].map((p) => getComputedStyle(el, p).boxShadow)
        return { outline, key: [outline && `${s.outlineWidth} ${s.outlineColor}`, s.boxShadow, s.backgroundColor, s.borderColor, ...pseudo].join('|') }
      }
      const focused = paint()
      // Same element, minus focus. `focus()` after a key press keeps `:focus-visible`.
      el.blur()
      const blurred = paint()
      el.focus({ preventScroll: true })
      const box = el.getBoundingClientRect()
      const inset = (lo: number, size: number, f: number) => lo + Math.min(Math.max(size * f, 1), size - 1)
      const onTop = [[0.5, 0.5], [0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.9, 0.9]].some(([fx, fy]) => {
        const hit = document.elementFromPoint(inset(box.left, box.width, fx), inset(box.top, box.height, fy))
        return !!hit && (el.contains(hit) || hit.contains(el))
      })
      const name = (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 40)
      return {
        id: `${el.tagName}|${name}|${Math.round(box.left)},${Math.round(box.top)}`,
        what: `${el.tagName.toLowerCase()} "${name}"`,
        hasIndicator: focused.key !== blurred.key,
        hasOutline: focused.outline,
        obscured: !onTop,
      }
    })
    if (!stop) continue
    if (seen.has(stop.id)) break
    seen.add(stop.id)
    stops.push({ what: stop.what, hasIndicator: stop.hasIndicator, hasOutline: stop.hasOutline, obscured: stop.obscured })
  }
  return stops
}
