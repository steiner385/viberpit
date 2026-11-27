/**
 * G18 (#1416) — the user-adaptation half of WCAG 2.2 AA, per top-level route.
 *
 * `shell-modes.spec.ts` asks "does each page hold at the sizes we designed for?".
 * This asks what happens when the READER changes the conditions:
 *
 *   - 400% zoom (SC 1.4.10 Reflow) and 200% zoom / text-only 200% (SC 1.4.4);
 *   - the text-spacing overrides (SC 1.4.12);
 *   - `prefers-reduced-motion` (SC 2.2.2, 2.3.3);
 *   - keyboard focus: an indicator at every stop (SC 2.4.7), never fully covered
 *     (SC 2.4.11);
 *   - forced colours (Windows High Contrast), where the indicator must survive
 *     the UA removing every shadow and background.
 *
 * It is the automated column of `docs/audits/wcag-2.2-aa-conformance.md`, and
 * only that: emulating a media feature is not running a screen reader, a
 * software keyboard or a switch. Those rows of the matrix stay "not verified"
 * until a person fills them in.
 *
 * A gap this gate found and this change did not fix is listed in `KNOWN_GAPS`
 * with its issue — asserted to still be a gap, so the entry cannot outlive the fix.
 */
import { test, expect, type Page } from '@playwright/test'
import { installSeededApi } from '../fixtures/seed'
import {
  contentOutsideViewport, expectContentWithinViewport, expectScrollPanesUsable, REFLOW_VIEWPORT,
  runningAnimations, starvedScrollPanes, TEXT_200_CSS, TEXT_SPACING_CSS, walkFocus, ZOOM_200_VIEWPORT,
} from '../support/adaptations'
import { publishEvidence } from '../support/evidence'
import { expectNoHorizontalOverflow, expectNoSilentTextClipping } from '../support/geometry'
import { openSeededRoute, ROUTES, type Route } from '../support/routes'

/**
 * Real, tracked, not silenced: at 320×256 the fixed chrome of these two pages
 * leaves the content pane less than one row. Each entry asserts the gap is STILL
 * there — fix the layout and the test goes red until the entry is deleted.
 */
const KNOWN_GAPS: Partial<Record<Route['path'], string>> = {
  '/sessions': '#3561: the dispatch dock keeps its full height and the session list gets ~8px',
  '/pipeline': '#3561: the page header and repo tabs leave the board ~24px',
}

const shot = async (page: Page) => page.screenshot({ animations: 'disabled' })

test.describe('Reflow — 400% zoom of 1280×1024 (SC 1.4.10)', () => {
  test.use({ viewport: REFLOW_VIEWPORT })

  for (const route of ROUTES) {
    test(`${route.name}: no sideways scrolling, nothing off-screen, content reachable`, async ({ page }, testInfo) => {
      await openSeededRoute(page, route)
      await expectNoHorizontalOverflow(page)
      await expectContentWithinViewport(page)

      const gap = KNOWN_GAPS[route.path]
      if (gap) {
        expect(await starvedScrollPanes(page), `known gap is fixed — delete its KNOWN_GAPS entry (${gap})`).not.toEqual([])
      } else {
        await expectScrollPanesUsable(page)
      }
      await publishEvidence(testInfo, `${route.name}-reflow-320x256.png`, await shot(page), 'image/png')
    })
  }
})

test.describe('Reflow — a 320px-wide window at phone height', () => {
  // The normative half of SC 1.4.10 for content that scrolls vertically: 320 CSS px wide.
  test.use({ viewport: { width: 320, height: 568 } })

  for (const route of ROUTES) {
    test(`${route.name}: holds at 320 wide with no exception`, async ({ page }) => {
      await openSeededRoute(page, route)
      await expectNoHorizontalOverflow(page)
      await expectContentWithinViewport(page)
      await expectScrollPanesUsable(page)
    })
  }
})

test.describe('Resize text — 200% browser zoom of 1280×800 (SC 1.4.4)', () => {
  test.use({ viewport: ZOOM_200_VIEWPORT })

  for (const route of ROUTES) {
    test(`${route.name}: nothing lost at 200%`, async ({ page }, testInfo) => {
      await openSeededRoute(page, route)
      await expectNoHorizontalOverflow(page)
      await expectContentWithinViewport(page)
      await expectScrollPanesUsable(page)
      await publishEvidence(testInfo, `${route.name}-zoom-200.png`, await shot(page), 'image/png')
    })
  }
})

test.describe('Resize text — text-only 200% at 1280×800 (SC 1.4.4)', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  for (const route of ROUTES) {
    test(`${route.name}: text doubles without clipping or loss`, async ({ page }, testInfo) => {
      await openSeededRoute(page, route)
      await page.addStyleTag({ content: TEXT_200_CSS })
      // The type scale is rem, so the override must actually have doubled the text.
      expect(await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize))).toBe(32)
      await expectContentWithinViewport(page)
      await expectNoSilentTextClipping(page)
      await expectScrollPanesUsable(page)
      await publishEvidence(testInfo, `${route.name}-text-200.png`, await shot(page), 'image/png')
    })
  }
})

test.describe('Text spacing overrides on a phone (SC 1.4.12)', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  for (const route of ROUTES) {
    test(`${route.name}: no text clipped, nothing pushed off-screen`, async ({ page }) => {
      await openSeededRoute(page, route)
      await page.addStyleTag({ content: TEXT_SPACING_CSS })
      await expectContentWithinViewport(page)
      await expectNoSilentTextClipping(page)
    })
  }
})

test.describe('Reduced motion (SC 2.2.2, 2.3.3)', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  for (const route of ROUTES) {
    test(`${route.name}: nothing animates under prefers-reduced-motion`, async ({ page }) => {
      await openSeededRoute(page, route) // the seed emulates `reduce`
      expect(await runningAnimations(page)).toEqual([])
    })
  }

  // The control: the same page, motion allowed, DOES animate (the working session's
  // pulse) — so the empty list above is the preference being honoured, not a probe
  // that cannot see animations.
  test('Sessions: the same page animates when motion is allowed', async ({ page }) => {
    await installSeededApi(page)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.goto('/sessions')
    await expect(page.getByText('Refactor the importer')).toBeVisible()
    await expect.poll(() => runningAnimations(page)).not.toEqual([])
  })
})

test.describe('Keyboard focus (SC 2.4.7, 2.4.11)', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  for (const route of ROUTES) {
    test(`${route.name}: every tab stop shows focus and is not covered`, async ({ page }) => {
      await openSeededRoute(page, route)
      const stops = await walkFocus(page)
      expect(stops.length, 'no tab stops reached').toBeGreaterThan(0)
      expect(stops.filter((s) => !s.hasIndicator).map((s) => s.what), 'tab stops with no visible focus indicator').toEqual([])
      expect(stops.filter((s) => s.obscured).map((s) => s.what), 'focused controls entirely covered by other content').toEqual([])
    })
  }
})

test.describe('Forced colours — Windows High Contrast', () => {
  test.use({ viewport: { width: 1280, height: 800 } })
  // WebKit has no forced-colors mode to emulate; this is a Chromium/Firefox (Windows) feature.
  test.skip(({ browserName }) => browserName === 'webkit', 'forced-colors is not implemented in WebKit')

  for (const route of ROUTES) {
    test(`${route.name}: focus is an outline at every tab stop`, async ({ page }, testInfo) => {
      await openSeededRoute(page, route)
      await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' })
      const stops = await walkFocus(page)
      expect(stops.length, 'no tab stops reached').toBeGreaterThan(0)
      // An outline specifically: the UA strips box-shadow rings and background fills here.
      expect(stops.filter((s) => !s.hasOutline).map((s) => s.what), 'tab stops with no outline in forced colours').toEqual([])
      await publishEvidence(testInfo, `${route.name}-forced-colors.png`, await shot(page), 'image/png')
    })
  }

  test('the current section is marked by more than a background fill', async ({ page }) => {
    await openSeededRoute(page, ROUTES[0])
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' })
    const current = page.getByRole('navigation', { name: 'Sections', exact: true }).locator('[aria-current="page"]')
    await expect(current).toHaveCount(1)
    expect(await current.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid')
  })
})

test.describe('These gates can fail', () => {
  // Same idea as `seeded-defects.spec.ts`: a deliberate defect in the real, seeded app,
  // and an inverted expectation — green while proving the check bites.
  test.use({ viewport: { width: 1024, height: 768 } })

  const seed = (page: Page, html: string) => page.evaluate((markup) => {
    const scratch = document.createElement('div')
    scratch.innerHTML = markup
    document.body.appendChild(scratch)
  }, html)

  test('content pushed past the viewport edge is reported', async ({ page }) => {
    await openSeededRoute(page, ROUTES[0])
    expect(await contentOutsideViewport(page)).toEqual([])
    await seed(page, '<button style="position:fixed;top:8px;left:980px;width:200px">Seeded off-screen</button>')
    expect((await contentOutsideViewport(page)).join('\n')).toContain('Seeded off-screen')
  })

  test('a scroll pane squeezed to nothing is reported', async ({ page }) => {
    await openSeededRoute(page, ROUTES[0])
    expect(await starvedScrollPanes(page)).toEqual([])
    await seed(page, '<div style="position:fixed;top:0;left:0;height:10px;overflow-y:auto"><div style="height:400px">rows</div></div>')
    expect(await starvedScrollPanes(page)).toHaveLength(1)
  })

  test('a control with its focus indicator removed is reported', async ({ page }) => {
    await openSeededRoute(page, ROUTES[0])
    // What `outline-none` with no ring looks like — and what every control was in forced colours.
    await page.addStyleTag({ content: '*:focus-visible { outline: none !important; box-shadow: none !important; background-color: inherit !important; border-color: inherit !important; }' })
    const stops = await walkFocus(page)
    expect(stops.length).toBeGreaterThan(0)
    expect(stops.filter((s) => !s.hasIndicator)).not.toEqual([])
  })
})
