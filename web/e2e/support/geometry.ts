/**
 * Layout/geometry assertions shared by the cross-browser gates (#1412, G14).
 *
 * These measure what a real engine laid out — bounding boxes, scroll extents —
 * rather than classes, because jsdom does no layout and a class contract keeps
 * passing after the variant that carries it stops compiling. The same helpers
 * back the seeded-defect specs, which prove each one FAILS when the layout is
 * deliberately broken (a gate that cannot fail is not a gate).
 *
 * Tolerances are deliberate: Chromium, Firefox and WebKit round sub-pixel boxes
 * differently, so exact-pixel equality would turn an engine difference into a red
 * build. Every helper allows `SUBPIXEL_PX` of slack and nothing more.
 */
import { expect, type Locator, type Page } from '@playwright/test'

/** Rounding slack between engines. One pixel absorbs layout rounding, not a real bug. */
export const SUBPIXEL_PX = 1

/** WCAG 2.2 SC 2.5.8 (AA): the floor for a pointer target on any pointer. */
export const MIN_TARGET_AA_PX = 24
/** The app's own touch floor (`--target`, docs/ACCESSIBILITY.md §4). */
export const MIN_TARGET_TOUCH_PX = 44

export interface Box { x: number; y: number; width: number; height: number }

/** The element's rendered box, failing loudly (not with a null deref) when it has none. */
export async function boxOf(locator: Locator, what = 'control'): Promise<Box> {
  // A remount between `toBeVisible()` and the read (the open-session hand-off swaps
  // the pane) momentarily yields no box on webkit/firefox, so poll before failing (#3542).
  let box: Box | null = null
  await expect
    .poll(async () => (box = await locator.first().boundingBox()), {
      message: `${what} has no box — it is not rendered or is display:none`,
    })
    .not.toBeNull()
  return box as unknown as Box
}

/** How far the page scrolls sideways beyond the viewport. 0 when it does not. */
export async function horizontalOverflowPx(page: Page): Promise<number> {
  return page.evaluate(() => {
    const doc = document.documentElement
    return Math.max(doc.scrollWidth - doc.clientWidth, document.body.scrollWidth - doc.clientWidth, 0)
  })
}

/** The page never scrolls sideways (a phone must not need a horizontal swipe). */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await horizontalOverflowPx(page)
  expect(overflow, `page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(SUBPIXEL_PX)
}

/** The control's box is at least `min` px on both axes (WCAG 2.2 2.5.8 / the app's 44px touch floor). */
export async function expectMinTarget(locator: Locator, min: number, what = 'control'): Promise<void> {
  const box = await boxOf(locator, what)
  expect(box.width + SUBPIXEL_PX, `${what} is ${box.width}px wide, needs ${min}`).toBeGreaterThanOrEqual(min)
  expect(box.height + SUBPIXEL_PX, `${what} is ${box.height}px tall, needs ${min}`).toBeGreaterThanOrEqual(min)
}

/** The control sits wholly inside the viewport — reachable without scrolling the page. */
export async function expectInViewport(page: Page, locator: Locator, what = 'control'): Promise<void> {
  const box = await boxOf(locator, what)
  const vp = page.viewportSize()
  if (!vp) throw new Error('page has no viewport size')
  expect(box.x, `${what} starts left of the viewport`).toBeGreaterThanOrEqual(-SUBPIXEL_PX)
  expect(box.y, `${what} starts above the viewport`).toBeGreaterThanOrEqual(-SUBPIXEL_PX)
  expect(box.x + box.width, `${what} runs past the right edge`).toBeLessThanOrEqual(vp.width + SUBPIXEL_PX)
  expect(box.y + box.height, `${what} runs past the bottom edge`).toBeLessThanOrEqual(vp.height + SUBPIXEL_PX)
}

/** Two regions that must not share pixels (rail vs content, list vs dock). */
export async function expectNoOverlap(a: Locator, b: Locator, what = 'regions'): Promise<void> {
  const [boxA, boxB] = [await boxOf(a, `${what} (first)`), await boxOf(b, `${what} (second)`)]
  const overlapX = Math.min(boxA.x + boxA.width, boxB.x + boxB.width) - Math.max(boxA.x, boxB.x)
  const overlapY = Math.min(boxA.y + boxA.height, boxB.y + boxB.height) - Math.max(boxA.y, boxB.y)
  const overlaps = overlapX > SUBPIXEL_PX && overlapY > SUBPIXEL_PX
  expect(overlaps, `${what} overlap by ${Math.round(overlapX)}×${Math.round(overlapY)}px`).toBe(false)
}

/**
 * Text that a fixed-size box clips. A visible, non-scrollable element whose content
 * is wider than its box and whose `overflow` hides the excess *and* has no ellipsis
 * is silently truncated — the reader loses characters with no cue.
 *
 * Returns descriptions of the offenders within `root` (the whole page by default).
 */
export async function silentlyClippedText(page: Page, root = 'body'): Promise<string[]> {
  return page.evaluate((selector) => {
    const offenders: string[] = []
    for (const el of document.querySelectorAll(`${selector} *`)) {
      if (!(el instanceof HTMLElement)) continue
      if (!el.childNodes.length || ![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent?.trim())) continue
      const style = getComputedStyle(el)
      if (style.visibility === 'hidden' || style.display === 'none') continue
      // Visually-hidden text (`sr-only`: a 1px clipped box) is clipped on purpose — it is
      // for a screen reader, and no sighted reader loses characters from it.
      if (el.clientWidth <= 1 && el.clientHeight <= 1) continue
      const clips = style.overflowX === 'hidden' || style.overflowX === 'clip'
      if (!clips || style.textOverflow === 'ellipsis') continue
      if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
        offenders.push(`${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 3).join('.')} "${(el.textContent ?? '').trim().slice(0, 40)}"`)
      }
    }
    return offenders
  }, root)
}

/** No text is silently clipped inside `root`. */
export async function expectNoSilentTextClipping(page: Page, root = 'body'): Promise<void> {
  const offenders = await silentlyClippedText(page, root)
  expect(offenders, `text clipped without an ellipsis: ${offenders.join('; ')}`).toEqual([])
}
